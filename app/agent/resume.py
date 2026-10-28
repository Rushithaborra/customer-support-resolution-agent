"""Wake a paused agent workflow after a human (or the expiry job) has decided.

The request side only queues a `resume` job (request_resume); the worker runs
resume_for_proposal, which may call the payment provider.

The resume value is only a signal. The graph re-reads the proposal's stored
status, so a stray or forged resume can't approve anything. Resuming a thread
that isn't waiting is a no-op, which makes every caller here safe to repeat.
"""

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.graph import AgentDeps, build_graph
from app.db.models import ActionProposal, Job, WorkflowRun
from app.integrations.payment import PaymentClient
from app.jobs import queue


def is_agent_run(run: WorkflowRun) -> bool:
    return run.thread_id.startswith("agent-")  # manual (staff) runs have no graph thread


def request_resume(db: Session, proposal_id: str, signal: str) -> Job | None:
    """Queue the resume (the caller commits). None for manual proposals: nothing is paused."""
    proposal = db.get(ActionProposal, proposal_id)
    run = db.get(WorkflowRun, proposal.run_id)
    if not is_agent_run(run):
        return None
    return queue.enqueue(db, "resume", run_id=run.id, payload={"proposal_id": proposal_id, "signal": signal},
                         dedupe_key=f"resume:{proposal_id}:{signal}")


def request_resume_superseded(db: Session, ticket_id: str) -> list[str]:
    """A new proposal on the ticket supersedes older ones; their paused threads finish as 'invalidated'."""
    stale = db.scalars(
        select(ActionProposal).join(WorkflowRun, ActionProposal.run_id == WorkflowRun.id)
        .where(WorkflowRun.ticket_id == ticket_id, WorkflowRun.status == "paused",
               ActionProposal.status == "superseded")
    ).all()
    for p in stale:
        request_resume(db, p.id, "superseded")
    return [p.id for p in stale]


def resume_for_proposal(db: Session, proposal_id: str, signal: str, *, checkpointer,
                        payment: PaymentClient | None) -> WorkflowRun | None:
    proposal = db.get(ActionProposal, proposal_id)
    run = db.get(WorkflowRun, proposal.run_id)
    if not is_agent_run(run):
        return None
    graph = build_graph(AgentDeps(db=db, payment=payment), checkpointer=checkpointer)
    config = {"configurable": {"thread_id": run.thread_id}}
    state = graph.get_state(config)
    if not state.next:
        return run  # not paused (already finished, or never got there)
    # A pending interrupt is answered with the signal; a thread whose worker died mid-way
    # through execute_refund/finalize (no interrupt left) carries on from its checkpoint.
    graph.invoke(Command(resume=signal) if state.interrupts else None, config, durability="sync")
    db.refresh(run)
    return run
