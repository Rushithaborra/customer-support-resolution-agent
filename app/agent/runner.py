"""Agent runs, split in two (Part 10):

- start_run (request side): checks, then records the run and its job in one
  transaction and returns. No LLM call, so the API answers 202 straight away.
- execute_run (worker side): drives the graph from its last durable checkpoint.
  A fresh run starts at the top; a run whose worker died carries on from the last
  completed node, so finished LLM steps are never redone.

Checkpoints say where the workflow is, not what money moved (rule 6): a node that
was interrupted mid-way re-runs, which is safe because only writer nodes write and
the refund step is idempotent per proposal.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.checkpoint import get_checkpointer
from app.agent.graph import AgentDeps, build_graph
from app.db.models import AuditEvent, Job, Ticket, WorkflowRun
from app.integrations.payment import PaymentClient
from app.integrations.shipping import ShippingClient
from app.jobs import queue
from app.llm.types import LLM
from app.policy.embedder import Embedder, get_embedder
from app.services.errors import Conflict, NotFound

# Money may be in flight: the agent must not re-investigate under a live proposal or refund.
_BLOCKED_TICKET_STATES = {"awaiting_approval", "awaiting_provider"}
_ACTIVE_RUN_STATES = ("queued", "running")


def start_run(db: Session, ticket_id: str, actor: str) -> tuple[WorkflowRun, Job]:
    # Lock the ticket so two concurrent starts can't both pass the checks below.
    ticket = db.get(Ticket, ticket_id, with_for_update=True)
    if ticket is None:
        raise NotFound("Ticket")
    if ticket.status in _BLOCKED_TICKET_STATES:
        raise Conflict("refund_in_flight", f"Ticket is {ticket.status}")
    active = db.scalar(select(WorkflowRun.id).where(WorkflowRun.ticket_id == ticket.id,
                                                    WorkflowRun.status.in_(_ACTIVE_RUN_STATES)))
    if active:
        db.rollback()
        raise Conflict("run_in_progress", f"Run {active} is already working on this ticket")

    run = WorkflowRun(ticket_id=ticket.id, thread_id=f"agent-{uuid.uuid4().hex}", status="queued")
    db.add(run)
    db.flush()
    db.add(AuditEvent(run_id=run.id, actor=actor, event_type="agent.queued", redacted_payload={"ticket_id": ticket.id}))
    job = queue.enqueue(db, "agent_run", run_id=run.id, dedupe_key=f"agent_run:{run.id}")
    db.commit()  # the run and its job become visible together
    return run, job


def execute_run(db: Session, deps: AgentDeps, checkpointer, run: WorkflowRun, job: Job) -> WorkflowRun:
    graph = build_graph(deps, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": run.thread_id}}
    state = graph.get_state(config)
    if state.values and (not state.next or state.interrupts):
        return run  # already finished or paused for approval: a re-delivered job has nothing to do

    run.status = "running"
    db.commit()
    start = None if state.next else {"run_id": run.id, "ticket_id": run.ticket_id}  # None = carry on
    # durability="sync": each step's checkpoint is written before the next step starts,
    # so a worker killed at any point loses at most the step it was in.
    for update in graph.stream(start, config, stream_mode="updates", durability="sync"):
        for node in update:
            event = "agent.paused" if node == "__interrupt__" else "node.completed"
            db.add(AuditEvent(run_id=run.id, actor="agent", event_type=event,
                              redacted_payload={} if node == "__interrupt__" else {"node": node}))
        queue.heartbeat(db, job)  # commits the events too
    db.refresh(run)
    return run


def run_agent(db: Session, llm: LLM, shipping: ShippingClient, ticket_id: str, actor: str,
              embedder: Embedder | None = None, checkpointer=None,
              payment: PaymentClient | None = None) -> WorkflowRun:
    """start_run + execute it right here, for scripts and tests. Errors are final (no retry)."""
    from app.jobs.worker import WorkerContext, process  # worker imports this module

    run, job = start_run(db, ticket_id, actor)
    ctx = WorkerContext(llm=llm, shipping=shipping, payment=payment, embedder=embedder or get_embedder(),
                        checkpointer=checkpointer or get_checkpointer())
    process(db, ctx, queue.claim_job(db, job.id, "inline"), raise_errors=True)
    return db.get(WorkflowRun, run.id, populate_existing=True)
