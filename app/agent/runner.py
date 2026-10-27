"""Start a workflow run for a ticket and execute the graph until it finishes or pauses for
approval (synchronously; Part 10 makes it async). Checkpoints are durable (Postgres)."""

import uuid

from sqlalchemy.orm import Session

from app.agent.checkpoint import get_checkpointer
from app.agent.graph import AgentDeps, build_graph
from app.db.models import AuditEvent, WorkflowRun
from app.integrations.shipping import ShippingClient
from app.llm.types import LLM
from app.policy.embedder import Embedder, get_embedder
from app.services.errors import Conflict
from app.services.resolution import get_ticket

# Money may be in flight: the agent must not re-investigate under a live proposal or refund.
_BLOCKED_TICKET_STATES = {"awaiting_approval", "awaiting_provider"}


def run_agent(db: Session, llm: LLM, shipping: ShippingClient, ticket_id: str, actor: str,
              embedder: Embedder | None = None, checkpointer=None) -> WorkflowRun:
    ticket = get_ticket(db, ticket_id)
    if ticket.status in _BLOCKED_TICKET_STATES:
        raise Conflict("refund_in_flight", f"Ticket is {ticket.status}")

    run = WorkflowRun(ticket_id=ticket.id, thread_id=f"agent-{uuid.uuid4().hex}", status="running")
    db.add(run)
    db.flush()
    db.add(AuditEvent(run_id=run.id, actor=actor, event_type="agent.started", redacted_payload={"ticket_id": ticket.id}))
    db.commit()

    graph = build_graph(AgentDeps(db=db, llm=llm, shipping=shipping, embedder=embedder or get_embedder()),
                        checkpointer=checkpointer or get_checkpointer())
    try:
        graph.invoke({"run_id": run.id, "ticket_id": ticket.id},
                     config={"configurable": {"thread_id": run.thread_id}})
    except Exception:
        db.rollback()
        run = db.get(WorkflowRun, run.id)
        run.status = "failed"
        db.add(AuditEvent(run_id=run.id, actor="system", event_type="agent.failed", redacted_payload={}))
        db.commit()
        raise
    db.refresh(run)
    return run
