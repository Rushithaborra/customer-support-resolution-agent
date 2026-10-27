"""Wake a paused agent workflow after a human (or the expiry job) has decided.

The resume value is only a signal. The graph re-reads the proposal's stored
status, so a stray or forged resume can't approve anything. Resuming a thread
that isn't waiting is a no-op, which makes every caller here safe to repeat.
"""

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.graph import AgentDeps, build_graph
from app.db.models import ActionProposal, WorkflowRun
from app.integrations.payment import PaymentClient


def is_agent_run(run: WorkflowRun) -> bool:
    return run.thread_id.startswith("agent-")  # manual (staff) runs have no graph thread


def resume_for_proposal(db: Session, proposal_id: str, signal: str, *, checkpointer,
                        payment: PaymentClient | None) -> WorkflowRun | None:
    proposal = db.get(ActionProposal, proposal_id)
    run = db.get(WorkflowRun, proposal.run_id)
    if not is_agent_run(run):
        return None
    graph = build_graph(AgentDeps(db=db, payment=payment), checkpointer=checkpointer)
    config = {"configurable": {"thread_id": run.thread_id}}
    if not graph.get_state(config).next:
        return run  # not paused (already finished, or never got there)
    graph.invoke(Command(resume=signal), config)
    db.refresh(run)
    return run


def resume_superseded(db: Session, ticket_id: str, *, checkpointer, payment: PaymentClient | None) -> list[str]:
    """A new proposal on the ticket supersedes older ones; their paused threads finish as 'invalidated'."""
    stale = db.scalars(
        select(ActionProposal).join(WorkflowRun, ActionProposal.run_id == WorkflowRun.id)
        .where(WorkflowRun.ticket_id == ticket_id, WorkflowRun.status == "paused",
               ActionProposal.status == "superseded")
    ).all()
    for p in stale:
        resume_for_proposal(db, p.id, "superseded", checkpointer=checkpointer, payment=payment)
    return [p.id for p in stale]
