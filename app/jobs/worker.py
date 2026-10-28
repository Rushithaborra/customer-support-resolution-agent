"""The worker: claim a job, run it, record the result. Many can run at once.

Failure handling:
- Retryable (LLM or provider briefly down, a DB blip, anything unexpected): back to
  the queue with backoff, up to max_attempts.
- Permanent (bad LLM config, a read-only violation, a missing row): dead-lettered now;
  retrying would only fail the same way.
- Killed process: nothing is recorded; the lease runs out and another worker takes over.
"""

import logging
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.agent.graph import AgentDeps, ReadOnlyViolation
from app.agent.resume import request_resume, resume_for_proposal
from app.agent.runner import execute_run
from app.db.models import Job, WorkflowRun
from app.integrations.payment import PaymentClient
from app.integrations.shipping import ShippingClient
from app.jobs import queue
from app.llm.types import LLM, LLMConfigError
from app.policy.embedder import Embedder
from app.services import refunds
from app.services.approvals import expire_stale
from app.services.errors import Conflict, NotFound

log = logging.getLogger("worker")

PERMANENT = (LLMConfigError, ReadOnlyViolation, NotFound, Conflict)
# Periodic jobs: kind → every N seconds. Each worker tries to schedule them; dedupe keeps one per slot.
PERIODIC = {"expire_sweep": 300, "reconcile_sweep": 60}


@dataclass
class WorkerContext:
    llm: LLM | None = None
    shipping: ShippingClient | None = None
    payment: PaymentClient | None = None
    embedder: Embedder | None = None
    checkpointer: object | None = None
    mode: str = "single"  # agent architecture for agent_run jobs
    now: Callable[[], datetime] = field(default=queue.utcnow)


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# ---------------------------------------------------------------- handlers


def _agent_run(db: Session, ctx: WorkerContext, job: Job) -> None:
    deps = AgentDeps(db=db, llm=ctx.llm, shipping=ctx.shipping, embedder=ctx.embedder, payment=ctx.payment,
                     mode=ctx.mode)
    execute_run(db, deps, ctx.checkpointer, db.get(WorkflowRun, job.run_id), job)


def _resume(db: Session, ctx: WorkerContext, job: Job) -> None:
    resume_for_proposal(db, job.payload["proposal_id"], job.payload["signal"],
                        checkpointer=ctx.checkpointer, payment=ctx.payment)


def _expire_sweep(db: Session, ctx: WorkerContext, job: Job) -> None:
    for proposal_id in expire_stale(db, ctx.now()):
        request_resume(db, proposal_id, "expired")
    db.commit()


def _reconcile_sweep(db: Session, ctx: WorkerContext, job: Job) -> None:
    refunds.reconcile_due(db, ctx.payment, ctx.now())


HANDLERS = {"agent_run": _agent_run, "resume": _resume, "expire_sweep": _expire_sweep,
            "reconcile_sweep": _reconcile_sweep}


# ---------------------------------------------------------------- running jobs


def process(db: Session, ctx: WorkerContext, job: Job, *, raise_errors: bool = False) -> Job:
    """Run one claimed job to success or failure. raise_errors: inline mode, no retries."""
    job_id = job.id
    try:
        HANDLERS[job.kind](db, ctx, job)
    except queue.LeaseLost:
        db.rollback()  # someone else owns it now; record nothing
        log.warning("lost the lease on %s", job_id)
        return db.get(Job, job_id, populate_existing=True)
    except Exception as e:
        db.rollback()
        job = db.get(Job, job_id, populate_existing=True)
        queue.fail(db, job, e, retryable=not raise_errors and not isinstance(e, PERMANENT), now=ctx.now())
        log.exception("job %s (%s) failed", job_id, job.kind)
        if raise_errors:
            raise
        return job
    job = db.get(Job, job_id, populate_existing=True)
    queue.succeed(db, job, ctx.now())
    return job


def run_once(db: Session, ctx: WorkerContext, worker_id: str) -> Job | None:
    job = queue.claim(db, worker_id, ctx.now())
    return process(db, ctx, job) if job else None


def drain(db: Session, ctx: WorkerContext, worker_id: str = "drain", limit: int = 100) -> list[Job]:
    """Run jobs until none are due (tests, `--once`)."""
    done = []
    while len(done) < limit and (job := run_once(db, ctx, worker_id)):
        done.append(job)
    return done


def schedule(db: Session, kind: str, now: datetime) -> Job:
    """One periodic job per time bucket, however many workers try to schedule it."""
    bucket = int(now.timestamp()) // PERIODIC[kind]
    job = queue.enqueue(db, kind, dedupe_key=f"{kind}:{bucket}", now=now)
    db.commit()
    return job


def serve(session_factory: Callable[[], Session], ctx: WorkerContext, worker_id: str,
          stop: threading.Event, poll_s: float = 1.0) -> None:
    """The long-running loop. `stop` is set by SIGINT/SIGTERM: the current job finishes first."""
    next_due = dict.fromkeys(PERIODIC, 0.0)
    while not stop.is_set():
        with session_factory() as db:
            now = ctx.now()
            for kind, every_s in PERIODIC.items():
                if now.timestamp() >= next_due[kind]:
                    schedule(db, kind, now)
                    next_due[kind] = now.timestamp() + every_s
            job = run_once(db, ctx, worker_id)
        if job:
            log.info("job %s %s -> %s (attempt %s)", job.id, job.kind, job.status, job.attempts)
        else:
            stop.wait(poll_s)
