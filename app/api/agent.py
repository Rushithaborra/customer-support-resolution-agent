"""Staff routes to start the agent on a ticket and follow it.

Starting only records the run and its job (202 Accepted); a worker
(`python -m scripts.worker`) does the work. Poll the run, or read its timeline.
"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.runner import start_run
from app.auth import Staff, get_current_staff
from app.db.models import AuditEvent, Job, WorkflowRun
from app.db.session import get_db
from app.schemas import AgentRunOut, JobOut, RunEventOut
from app.services.errors import NotFound

router = APIRouter(prefix="/staff", tags=["agent"])


def _run_out(db: Session, run: WorkflowRun) -> AgentRunOut:
    job = db.scalars(select(Job).where(Job.run_id == run.id).order_by(Job.created_at.desc())).first()
    return AgentRunOut.model_validate(run).model_copy(update={"job": JobOut.model_validate(job) if job else None})


def _get_run(db: Session, run_id: str) -> WorkflowRun:
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise NotFound("Run")
    return run


@router.post("/tickets/{ticket_id}/agent-runs", response_model=AgentRunOut, status_code=202)
def start(ticket_id: str, response: Response, staff: Staff = Depends(get_current_staff),
          db: Session = Depends(get_db)):
    run, _ = start_run(db, ticket_id, actor=staff.actor)
    response.headers["Location"] = f"/staff/agent-runs/{run.id}"
    return _run_out(db, run)


@router.get("/agent-runs/{run_id}", response_model=AgentRunOut)
def get(run_id: str, _: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    return _run_out(db, _get_run(db, run_id))


@router.get("/agent-runs/{run_id}/events", response_model=list[RunEventOut])
def events(run_id: str, _: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    """The run's timeline, oldest first: queued, claimed, each node, paused, resumed, finished."""
    run = _get_run(db, run_id)
    return db.scalars(select(AuditEvent).where(AuditEvent.run_id == run.id).order_by(AuditEvent.id)).all()
