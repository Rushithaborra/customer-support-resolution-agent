"""Queue semantics: SKIP LOCKED, leases, backoff, dead letters, dedupe, the expiry sweep."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.agent.runner import start_run
from app.db.models import ActionProposal, Job, Ticket, WorkflowRun
from app.db.session import SessionLocal
from app.jobs import queue
from app.jobs.worker import schedule_sweep
from app.llm.fake import ScriptedLLM
from app.llm.types import LLMConfigError
from tests.agent_scripts import draft, open_ticket, refund_script
from tests.conftest import STAFF

ACTOR = f"staff:{STAFF}"


def test_two_workers_claiming_at_once_get_different_jobs():
    """Real, separate connections: while A holds its row lock, B skips that row instead of waiting."""
    old = queue.utcnow() - timedelta(days=3650)  # sorts first, ahead of anything else due
    with SessionLocal() as setup:
        ids = [queue.enqueue(setup, "expire_sweep", dedupe_key=f"test-skip-locked-{i}", now=old).id for i in range(2)]
        setup.commit()
    try:
        with SessionLocal() as a, SessionLocal() as b:
            got_a = queue.next_claimable(a, queue.utcnow())  # row locked, transaction still open
            got_b = queue.next_claimable(b, queue.utcnow())
            assert {got_a.id, got_b.id} == set(ids)
            a.rollback(), b.rollback()
    finally:
        with SessionLocal() as cleanup:
            cleanup.execute(delete(Job).where(Job.id.in_(ids)))
            cleanup.commit()


def test_live_lease_is_kept_and_expired_lease_is_taken_over(db):
    job = queue.enqueue(db, "expire_sweep")
    db.commit()
    assert queue.claim(db, "worker-a").id == job.id
    assert queue.claim(db, "worker-b") is None
    later = queue.utcnow() + queue.LEASE + timedelta(seconds=1)
    taken = queue.claim(db, "worker-b", later)
    assert (taken.id, taken.locked_by, taken.attempts) == (job.id, "worker-b", 2)


def test_heartbeat_from_a_worker_that_lost_its_lease_stops_it(db):
    job = queue.enqueue(db, "expire_sweep")
    db.commit()
    mine = queue.claim(db, "worker-a")
    stale_view = Job(id=mine.id, locked_by="worker-a")
    queue.claim(db, "worker-b", queue.utcnow() + queue.LEASE + timedelta(seconds=1))
    with pytest.raises(queue.LeaseLost):
        queue.heartbeat(db, stale_view)


def test_failures_back_off_then_dead_letter_and_escalate(client, db, work):
    tid = open_ticket(client, "refund?", "ord_1001")
    run, job = start_run(db, tid, ACTOR)
    t0 = queue.utcnow()

    work(ScriptedLLM([ConnectionError("blip")]), now=t0)
    j = db.get(Job, job.id)
    assert (j.status, j.attempts, j.run_after - t0) == ("queued", 1, timedelta(seconds=10))
    assert j.last_error.startswith("ConnectionError")
    assert db.get(WorkflowRun, run.id).status == "queued"
    assert work(ScriptedLLM([]), now=t0 + timedelta(seconds=9)) == []  # not due yet

    work(ScriptedLLM([ConnectionError("blip")]), now=t0 + timedelta(seconds=11))
    assert (db.get(Job, job.id).attempts, db.get(Job, job.id).run_after - t0) == (2, timedelta(seconds=31))
    work(ScriptedLLM([ConnectionError("blip")]), now=t0 + timedelta(seconds=32))

    dead = db.get(Job, job.id)
    assert (dead.status, dead.attempts) == ("dead", 3)
    assert db.get(WorkflowRun, run.id).status == "failed"
    assert db.get(Ticket, tid).status == "escalated"  # a human picks it up


def test_permanent_error_is_dead_on_first_attempt(client, db, work):
    run, job = start_run(db, open_ticket(client, "refund?", "ord_1001"), ACTOR)
    work(ScriptedLLM([LLMConfigError("401: bad key")]))
    assert (db.get(Job, job.id).status, db.get(Job, job.id).attempts) == ("dead", 1)
    assert db.get(WorkflowRun, run.id).status == "failed"


def test_same_dedupe_key_gives_one_job(db):
    a = queue.enqueue(db, "expire_sweep", dedupe_key="once")
    b = queue.enqueue(db, "expire_sweep", dedupe_key="once")
    assert a.id == b.id and db.query(Job).filter_by(dedupe_key="once").count() == 1


def test_sweep_is_scheduled_once_per_bucket(db):
    t = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)  # a 5-minute bucket boundary
    first = schedule_sweep(db, t)
    assert schedule_sweep(db, t + timedelta(minutes=4)).id == first.id  # second worker, same bucket
    assert schedule_sweep(db, t + timedelta(minutes=5)).id != first.id  # next bucket


def test_sweep_job_expires_and_finishes_paused_runs(agent, client, db, work):
    run = agent(open_ticket(client, "refund?", "ord_1001"), refund_script(draft("ord_1001")))
    p = db.get(ActionProposal, run.outcome["proposal_id"])
    queue.enqueue(db, "expire_sweep")
    db.commit()
    jobs = work(now=queue.utcnow() + timedelta(hours=49))  # sweep, then the resume it queued
    assert [j.kind for j in jobs] == ["expire_sweep", "resume"]
    assert db.get(ActionProposal, p.id).status == "expired"
    finished = db.get(WorkflowRun, run.id)
    assert (finished.status, finished.outcome["decision"]) == ("escalated", "expired")
