"""The per-ticket agent workflow (single-agent baseline).

  load_ticket → classify → resolve_order → investigate → summarize → route → record
                    └──────────── early exit (route already set) ─────────────┘
                                     route=propose_refund → draft_proposal → validate_proposal → record

Two rules shape it:
- Code picks the route. The model's recommended next step is recorded and
  compared, but routing reads code facts (ownership, eligibility, damage window).
- Only `record` writes. Every other node is wrapped read-only, so a node that
  re-runs on resume (Part 9) can't duplicate a side effect.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.agent.loop import run_tool_loop
from app.agent.prompts import INVESTIGATE_SYSTEM, TRIAGE_SYSTEM, wrap_customer_message
from app.agent.schemas import InvestigationSummary, RefundProposalDraft, TicketTriage
from app.agent.tools import ToolContext
from app.db.models import ActionProposal, AuditEvent, Ticket, WorkflowRun
from app.domain.eligibility import assess_damage, evaluate_refund
from app.domain.proposal_rules import validate_refund_draft
from app.integrations.shipping import ShippingClient
from app.llm.structured import Parsed, structured_call
from app.llm.types import LLM, Usage
from app.services.errors import NotFound
from app.services.facts import load_order_facts
from app.services.orders import get_owned_order
from app.services.proposals import build_refund_payload, create_refund_proposal

WRITER_NODES = {"record"}


class AgentState(TypedDict, total=False):
    # Plain JSON-able values only, so checkpoints serialize cleanly (Part 9: Postgres).
    run_id: str
    ticket_id: str
    customer_id: str
    message: str
    form_order_id: str | None
    triage: dict | None
    order_id: str | None
    trace: list[dict]
    audit: list[dict]
    investigation_text: str
    summary: dict | None
    model_suggestion: str | None
    route: str | None
    route_reason: str | None
    agreed: bool | None
    proposal_draft: dict | None
    proposal_violations: list[dict] | None
    proposal_payload: dict | None
    usage: dict
    models: list[str]


@dataclass
class AgentDeps:
    db: Session
    llm: LLM
    shipping: ShippingClient
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _add_usage(state: AgentState, usage: Usage, models: list[str]) -> dict:
    u = state.get("usage") or {"input_tokens": 0, "output_tokens": 0}
    return {"usage": {"input_tokens": u["input_tokens"] + usage.input_tokens,
                      "output_tokens": u["output_tokens"] + usage.output_tokens},
            "models": (state.get("models") or []) + models}


def _escalate(reason: str) -> dict:
    return {"route": "escalate", "route_reason": reason}


# ---------------------------------------------------------------- nodes


def _nodes(deps: AgentDeps) -> dict[str, Callable[[AgentState], dict]]:
    db, llm = deps.db, deps.llm

    def load_ticket(state: AgentState) -> dict:
        ticket = db.get(Ticket, state["ticket_id"])
        if ticket is None:
            raise NotFound("Ticket")
        out = {"customer_id": ticket.customer_id, "message": ticket.customer_message,
               "form_order_id": ticket.order_id}
        db.commit()  # end the read transaction before the first LLM call
        return out

    def classify(state: AgentState) -> dict:
        r = structured_call(llm, system=TRIAGE_SYSTEM, model=TicketTriage,
                            messages=[{"role": "user", "content": wrap_customer_message(state["message"])}])
        if not isinstance(r, Parsed):
            return {**_escalate(f"triage_failed:{r.reason}"), **_add_usage(state, r.usage, [])}
        return {"triage": r.value.model_dump(), **_add_usage(state, r.usage, [r.model])}

    def resolve_order(state: AgentState) -> dict:
        """Code decides which order this is about, and whether the customer owns it."""
        t = state["triage"]
        if t["needs_human"] or t["category"] == "unclassified":
            return _escalate("needs_human" if t["needs_human"] else "unclassified")
        form, text = state.get("form_order_id"), t["order_id"]
        if form and text and form != text:
            return _escalate("order_mismatch")
        order_id = form or text
        if order_id is None:  # every category that reaches here is order-specific
            return _escalate("no_order")
        try:
            get_owned_order(db, state["customer_id"], order_id)
        except NotFound:
            return {"route": "order_not_found", "route_reason": "not_found", "order_id": None}
        finally:
            db.commit()
        return {"order_id": order_id}

    def investigate(state: AgentState) -> dict:
        ctx = ToolContext(db=db, customer_id=state["customer_id"], shipping=deps.shipping,
                          run_id=state["run_id"], audit_buffer=[])
        r = run_tool_loop(llm, ctx, system=INVESTIGATE_SYSTEM, user_message=state["message"],
                          preface=f"System-verified order under investigation: {state['order_id']}")
        out = {"trace": [asdict(t) for t in r.trace], "audit": ctx.audit_buffer,
               "investigation_text": r.text, **_add_usage(state, r.usage, r.models)}
        if r.stop != "end_turn":
            out.update(_escalate(f"investigation_{r.stop}"))
        return out

    def summarize(state: AgentState) -> dict:
        calls = "\n".join(f"- {t['tool']}({t['input']}): {'ok' if t['ok'] else t['code']}" for t in state["trace"])
        content = (f"{wrap_customer_message(state['message'])}\n\n"
                   f"Tool calls made:\n{calls or '- none'}\n\n"
                   f"Investigator's notes (derived from tool results):\n{state['investigation_text']}")
        r = structured_call(llm, system=INVESTIGATE_SYSTEM, model=InvestigationSummary,
                            messages=[{"role": "user", "content": content}])
        if not isinstance(r, Parsed):
            return {**_escalate(f"summary_failed:{r.reason}"), **_add_usage(state, r.usage, [])}
        return {"summary": r.value.model_dump(), "model_suggestion": r.value.recommended_next_step,
                **_add_usage(state, r.usage, [r.model])}

    def route(state: AgentState) -> dict:
        """Deterministic. Reads fresh facts; the model's suggestion is only compared."""
        category = state["triage"]["category"]
        facts = load_order_facts(db, state["order_id"])
        db.commit()
        if category == "refund_request":
            d = evaluate_refund(facts)
            chosen = ("propose_refund", "eligible") if d.eligible else ("explain_policy", d.reason_code)
        elif category == "order_status":
            chosen = ("report_order_status", "order_status")
        else:  # damaged_item
            dmg = assess_damage(facts, deps.now())
            chosen = ("escalate" if dmg.recommendation == "escalate" else "explain_policy", dmg.reason_code)
        return {"route": chosen[0], "route_reason": chosen[1],
                "agreed": state.get("model_suggestion") == chosen[0]}

    def draft_proposal(state: AgentState) -> dict:
        calls = "\n".join(f"- {t['tool']}({t['input']}): {'ok' if t['ok'] else t['code']}" for t in state["trace"])
        findings = "\n".join(f"- {f['statement']} [{f['evidence_ref']}]" for f in state["summary"]["findings"])
        content = (f"Draft a refund proposal for order {state['order_id']}.\n"
                   f"Cite as evidence_refs only tool calls listed below that succeeded.\n\n"
                   f"Tool calls made:\n{calls}\n\nFindings:\n{findings}\n\n"
                   f"Investigator's notes:\n{state['investigation_text']}")
        r = structured_call(llm, system=INVESTIGATE_SYSTEM, model=RefundProposalDraft,
                            messages=[{"role": "user", "content": content}])
        if not isinstance(r, Parsed):
            return {**_escalate(f"proposal_draft_failed:{r.reason}"), **_add_usage(state, r.usage, [])}
        return {"proposal_draft": r.value.model_dump(), **_add_usage(state, r.usage, [r.model])}

    def validate_proposal(state: AgentState) -> dict:
        """Code checks the draft against fresh facts and this run's own tool trace.
        The payload that goes forward is built by code; the draft only contributes refs and rationale."""
        draft = state["proposal_draft"]
        facts = load_order_facts(db, state["order_id"])
        db.commit()
        decision = evaluate_refund(facts)
        violations = validate_refund_draft(draft, resolved_order_id=state["order_id"], decision=decision,
                                           currency=facts.currency, trace=state["trace"])
        if violations:
            return {**_escalate(f"proposal_rejected:{violations[0].code}"),
                    "proposal_violations": [{"code": v.code, "detail": v.detail} for v in violations]}
        payload = build_refund_payload(state["ticket_id"], facts, decision, evidence_refs=draft["evidence_refs"],
                                       proposed_by="agent", rationale=draft["rationale"])
        return {"proposal_payload": payload}

    def record(state: AgentState) -> dict:
        """The only writer. Idempotent: sets fields, and reuses this run's proposal if one exists."""
        run = db.get(WorkflowRun, state["run_id"])
        ticket = db.get(Ticket, state["ticket_id"])
        route_, reason = state["route"], state["route_reason"]
        triage, summary = state.get("triage"), state.get("summary")

        run.route, run.route_reason = route_, reason
        run.status = "escalated" if route_ == "escalate" else "completed"
        run.completed_at = deps.now()

        proposal_id = None
        payload = state.get("proposal_payload")
        if route_ == "propose_refund" and payload:
            existing = db.scalar(select(ActionProposal).where(ActionProposal.run_id == run.id))
            proposal = existing or create_refund_proposal(db, run=run, ticket=ticket, payload=payload, actor="agent")
            proposal_id, run.status, run.completed_at = proposal.id, "paused", None  # awaiting a human

        run.outcome = {"triage": triage, "order_id": state.get("order_id"), "summary": summary,
                       "trace": state.get("trace", []), "model_suggestion": state.get("model_suggestion"),
                       "agreed": state.get("agreed"), "usage": state.get("usage"), "models": state.get("models"),
                       "proposal_draft": state.get("proposal_draft"),
                       "proposal_violations": state.get("proposal_violations"), "proposal_id": proposal_id}

        if triage and ticket.category == "unclassified":  # never overwrite a human's classification
            ticket.category = triage["category"]
        ticket.summary = f"Agent route: {route_} ({reason})"
        if route_ == "escalate" and ticket.status == "open":
            ticket.status = "escalated"

        for a in state.get("audit", []):
            db.add(AuditEvent(run_id=run.id, actor="agent", event_type="tool.called", redacted_payload=a))
        db.add(AuditEvent(run_id=run.id, actor="agent", event_type="agent.routed",
                          redacted_payload={"route": route_, "reason": reason, "agreed": state.get("agreed")}))
        db.commit()
        return {}

    return {"load_ticket": load_ticket, "classify": classify, "resolve_order": resolve_order,
            "investigate": investigate, "summarize": summarize, "route": route,
            "draft_proposal": draft_proposal, "validate_proposal": validate_proposal, "record": record}


# ---------------------------------------------------------------- read-only guard


class ReadOnlyViolation(RuntimeError):
    pass


def read_only(name: str, fn: Callable[[AgentState], dict], db: Session) -> Callable[[AgentState], dict]:
    """Fail loudly if a non-writer node tries to flush any INSERT/UPDATE/DELETE."""
    def deny(session, _ctx, _instances):
        if session.new or session.dirty or session.deleted:
            raise ReadOnlyViolation(f"node '{name}' attempted a database write")

    def wrapped(state: AgentState) -> dict:
        event.listen(db, "before_flush", deny)
        try:
            return fn(state)
        finally:
            event.remove(db, "before_flush", deny)
    return wrapped


# ---------------------------------------------------------------- graph


def _next_or_record(nxt: str) -> Callable[[AgentState], str]:
    return lambda s: "record" if s.get("route") else nxt


def _if_proposing(nxt: str) -> Callable[[AgentState], str]:
    return lambda s: nxt if s.get("route") == "propose_refund" else "record"


def build_graph(deps: AgentDeps, checkpointer=None):
    g = StateGraph(AgentState)
    for name, fn in _nodes(deps).items():
        g.add_node(name, fn if name in WRITER_NODES else read_only(name, fn, deps.db))
    g.add_edge(START, "load_ticket")
    g.add_edge("load_ticket", "classify")
    g.add_conditional_edges("classify", _next_or_record("resolve_order"), ["resolve_order", "record"])
    g.add_conditional_edges("resolve_order", _next_or_record("investigate"), ["investigate", "record"])
    g.add_conditional_edges("investigate", _next_or_record("summarize"), ["summarize", "record"])
    g.add_conditional_edges("summarize", _next_or_record("route"), ["route", "record"])
    g.add_conditional_edges("route", _if_proposing("draft_proposal"), ["draft_proposal", "record"])
    g.add_conditional_edges("draft_proposal", _if_proposing("validate_proposal"), ["validate_proposal", "record"])
    g.add_edge("validate_proposal", "record")
    g.add_edge("record", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())
