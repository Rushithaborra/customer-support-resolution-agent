"""A durable job queue in Postgres.

  request ─► enqueue (same transaction as the business row) ─► commit
  worker  ─► claim: FOR UPDATE SKIP LOCKED, set a lease, commit ─► do the work
          ─► heartbeat after each step (extends the lease) ─► succeed / fail

Rules:
- enqueue never commits. The caller commits the job together with the row it
  belongs to (outbox), so there is never a run without a job or a job without a run.
- Every transaction here is short. Nothing is held open while the work runs.
- A lease that runs out means the worker died; the next claim takes the job over.
  A job that keeps dying is buried after max_attempts, so a poison job can't loop forever.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, Job, Ticket, WorkflowRun, new_id

LEASE = timedelta(minutes=10)
BACKOFF_BASE_S = 5


class LeaseLost(Exception):
    """Another worker took this job over (our lease ran out). Stop without recording anything."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(db: Session, kind: str, *, run_id: str | None = None, payload: dict | None = None,
            dedupe_key: str | None = None, now: datetime | None = None) -> Job:
    """Add a job inside the caller's transaction. A repeated dedupe_key returns the existing job."""
    now = now or utcnow()
    values = {"kind": kind, "run_id": run_id, "payload": payload or {}, "status": "queued", "attempts": 0,
              "max_attempts": 3, "run_after": now, "created_at": now, "dedupe_key": dedupe_key}
    job_id = db.execute(insert(Job).values(id=new_id("job"), **values)
                        .on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(Job.id)).scalar()
    if job_id is None:
        return db.scalars(select(Job).where(Job.dedupe_key == dedupe_key)).one()
    return db.get(Job, job_id)


def next_claimable(db: Session, now: datetime) -> Job | None:
    """Lock the oldest job that is due, or whose worker's lease ran out. SKIP LOCKED: a row
    another worker is claiming right now is skipped, not waited on."""
    return db.scalars(
        select(Job)
        .where(or_(and_(Job.status == "queued", Job.run_after <= now),
                   and_(Job.status == "running", Job.locked_until < now)))
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()


def claim(db: Session, worker_id: str, now: datetime | None = None) -> Job | None:
    now = now or utcnow()
    while True:
        job = next_claimable(db, now)
        if job is None:
            db.commit()
            return None
        if job.status == "running" and job.attempts >= job.max_attempts:
            bury(db, job, "worker died on every attempt", now)  # poison job
            continue
        return _take(db, job, worker_id, now)


def claim_job(db: Session, job_id: str, worker_id: str, now: datetime | None = None) -> Job:
    """Claim one specific job (inline runs in scripts and tests)."""
    job = db.get(Job, job_id, with_for_update=True, populate_existing=True)
    return _take(db, job, worker_id, now or utcnow())


def _take(db: Session, job: Job, worker_id: str, now: datetime) -> Job:
    taken_over = job.status == "running"
    job.status, job.locked_by, job.locked_until = "running", worker_id, now + LEASE
    job.attempts += 1
    if job.run_id:
        db.add(AuditEvent(run_id=job.run_id, actor="system", event_type="job.claimed",
                          redacted_payload={"job_id": job.id, "kind": job.kind, "attempt": job.attempts,
                                            "worker": worker_id, "taken_over": taken_over}))
    db.commit()
    return job


def heartbeat(db: Session, job: Job, now: datetime | None = None) -> None:
    """Extend the lease, but only if it is still ours."""
    still_ours = db.execute(
        update(Job).where(Job.id == job.id, Job.status == "running", Job.locked_by == job.locked_by)
        .values(locked_until=(now or utcnow()) + LEASE)).rowcount
    db.commit()
    if not still_ours:
        raise LeaseLost(job.id)


def succeed(db: Session, job: Job, now: datetime | None = None) -> None:
    job.status, job.finished_at, job.locked_by, job.locked_until = "succeeded", now or utcnow(), None, None
    db.commit()


def fail(db: Session, job: Job, error: BaseException, *, retryable: bool, now: datetime | None = None) -> None:
    """Retry later with exponential backoff, or bury once attempts are used up."""
    now = now or utcnow()
    reason = f"{type(error).__name__}: {error}"[:500]
    if not retryable or job.attempts >= job.max_attempts:
        bury(db, job, reason, now)
        return
    job.status, job.locked_by, job.locked_until = "queued", None, None
    job.last_error = reason
    job.run_after = now + timedelta(seconds=BACKOFF_BASE_S * 2 ** job.attempts)
    if job.kind == "agent_run" and job.run_id:
        db.get(WorkflowRun, job.run_id).status = "queued"
    db.commit()


def bury(db: Session, job: Job, reason: str, now: datetime) -> None:
    """Dead-letter: stop retrying. A dead agent run fails and its ticket goes to a human."""
    job.status, job.finished_at, job.locked_by, job.locked_until = "dead", now, None, None
    job.last_error = reason[:500]
    if job.run_id:
        run = db.get(WorkflowRun, job.run_id)
        if job.kind == "agent_run" and run.status in ("queued", "running"):
            run.status, run.completed_at = "failed", now
            ticket = db.get(Ticket, run.ticket_id)
            if ticket.status == "open":
                ticket.status = "escalated"
        db.add(AuditEvent(run_id=job.run_id, actor="system", event_type="job.dead_lettered",
                          redacted_payload={"job_id": job.id, "kind": job.kind, "attempts": job.attempts,
                                            "error": reason.split(":")[0]}))
    db.commit()
