"""Refund proposals: one builder for both the staff path (Part 4) and the agent path (Part 7).

The payload is always built from code facts. The amount is the rules' refundable
amount, never a number a person or a model typed. Evidence refs, cited policy
sections, who proposed it and why are inside the payload, so they're inside the
hash the approval binds to.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ActionProposal, AuditEvent, Ticket, WorkflowRun
from app.domain.eligibility import OrderFacts, RefundDecision
from app.domain.hashing import payload_hash
from app.domain.policy import POLICY_VERSION


def build_refund_payload(ticket_id: str, facts: OrderFacts, decision: RefundDecision, *,
                         evidence_refs: list[str], proposed_by: str, rationale: str,
                         policy_citations: list[str]) -> dict:
    if not policy_citations:
        raise ValueError("a refund proposal must cite the policy it applies")
    if not decision.eligible:
        raise ValueError("refund payloads are only built for eligible decisions")
    r = facts.latest_return
    return {
        "action": "refund",
        "ticket_id": ticket_id,
        "order_id": facts.order_id,
        "amount_minor": decision.refundable_minor,
        "currency": facts.currency,
        "reason_code": decision.reason_code,
        "policy_version": POLICY_VERSION,
        "evidence": {"return_id": r.return_id, "return_status": r.status,
                     "inspection_status": r.inspection_status, "received_at": r.received_at.isoformat()},
        "evidence_refs": list(evidence_refs),
        "policy_citations": list(policy_citations),  # e.g. refund_policy@v2#section_1
        "proposed_by": proposed_by,
        "rationale": rationale,
    }


def create_refund_proposal(db: Session, *, run: WorkflowRun, ticket: Ticket, payload: dict,
                           actor: str) -> ActionProposal:
    """Insert a pending proposal. Does NOT commit: the caller owns the transaction."""
    # One live proposal per ticket: older unexecuted ones can no longer be approved or run.
    stale = db.scalars(
        select(ActionProposal).join(WorkflowRun, ActionProposal.run_id == WorkflowRun.id)
        .where(WorkflowRun.ticket_id == ticket.id,
               ActionProposal.status.in_(["pending_approval", "approved"]))
    ).all()
    for p in stale:
        p.status = "superseded"

    proposal = ActionProposal(run_id=run.id, action_type="refund", payload=payload,
                              payload_hash=payload_hash(payload))
    db.add(proposal)
    db.flush()
    ticket.status = "awaiting_approval"
    run.status = "paused"
    db.add(AuditEvent(run_id=run.id, actor=actor, event_type="proposal.created", redacted_payload={
        "proposal_id": proposal.id, "amount_minor": payload["amount_minor"],
        "payload_hash": proposal.payload_hash, "proposed_by": payload["proposed_by"],
        "superseded": [p.id for p in stale]}))
    return proposal
