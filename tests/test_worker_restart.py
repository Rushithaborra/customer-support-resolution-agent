"""Part 10 done-when: a run survives a worker restart and resumes.

A "killed worker" is a BaseException raised mid-job: nothing in the worker catches it, so the
job is left `running` with its lease, exactly as if the process had died. A second worker, on a
brand-new checkpointer, may only take over once that lease has run out.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agent.checkpoint import open_checkpointer
from app.agent.runner import start_run
from app.db.models import ActionProposal, AuditEvent, Job, RefundOperation, Ticket, WorkflowRun
from app.integrations.payment import PaymentClient
from app.jobs import queue
from app.llm.fake import ScriptedLLM, text_turn, tool_turn
from tests.agent_scripts import draft, open_ticket, refund_script, summary, triage
from tests.conftest import as_reviewer
from tests.test_approval_restart import graph_next, refunds_at_provider


class WorkerKilled(BaseException):
    """Stands in for kill -9: escapes every `except Exception`."""


@pytest.fixture
def fresh_saver():
    pools = []

    def make():
        saver, pool = open_checkpointer()
        pools.append(pool)
        return saver
    yield make
    for p in pools:
        p.close()


def events(db, run_id: str, event_type: str) -> list[dict]:
    return [e.redacted_payload for e in db.scalars(
        select(AuditEvent).where(AuditEvent.run_id == run_id, AuditEvent.event_type == event_type)
        .order_by(AuditEvent.id))]


def after_lease():
    return queue.utcnow() + queue.LEASE + timedelta(seconds=1)


def test_run_killed_mid_graph_is_taken_over_and_continues(client, db, work, fresh_saver):
    tid = open_ticket(client, "I returned my headphones, where's my refund?", "ord_1001")
    run, job = start_run(db, tid, "staff:staff|priya")

    # Worker A gets through triage and investigation, then dies inside summarize.
    first = ScriptedLLM([triage("refund_request", "ord_1001"),
                         tool_turn(("check_refund_eligibility", {"order_id": "ord_1001"})),
                         text_turn("eligible for 249900"), WorkerKilled()])
    with pytest.raises(WorkerKilled):
        work(first, saver=fresh_saver(), worker_id="worker-a")
    db.rollback()  # the dead process's open transaction is gone
    stuck = db.get(Job, job.id, populate_existing=True)
    assert (stuck.status, stuck.attempts, stuck.locked_by) == ("running", 1, "worker-a")
    assert db.get(WorkflowRun, run.id, populate_existing=True).status == "running"

    # While A's lease is live, nobody steals the job.
    assert work(ScriptedLLM([]), worker_id="worker-b") == []

    # After the lease: a new worker, new checkpointer, and only the LLM turns that are left.
    rest = ScriptedLLM([summary("propose_refund", "check_refund_eligibility:ord_1001"), draft("ord_1001")])
    [done] = work(rest, saver=fresh_saver(), now=after_lease(), worker_id="worker-b")
    assert (done.id, done.status, done.attempts) == (job.id, "succeeded", 2)

    finished = db.get(WorkflowRun, run.id, populate_existing=True)
    assert (finished.status, finished.route) == ("paused", "propose_refund")
    assert db.scalars(select(ActionProposal).where(ActionProposal.run_id == run.id)).all() != []
    assert len(rest.requests) == 2  # triage and investigation were not redone
    assert graph_next(db, run.thread_id) == ("await_approval",)

    claims = events(db, run.id, "job.claimed")
    assert [(c["worker"], c["attempt"], c["taken_over"]) for c in claims] == \
        [("worker-a", 1, False), ("worker-b", 2, True)]
    nodes = [e["node"] for e in events(db, run.id, "node.completed")]
    assert nodes.count("investigate") == 1 and nodes.count("summarize") == 1


class DiesAfterSending(PaymentClient):
    """The provider accepts the refund, then the worker dies before recording the answer."""

    def create_refund(self, **kw):
        super().create_refund(**kw)
        raise WorkerKilled()


def test_worker_killed_inside_execute_refund_refunds_once(agent, client, staff_client, db, work, mock_admin,
                                                          service_client, fresh_saver):
    run = agent(open_ticket(client, "refund please", "ord_1001"), refund_script(draft("ord_1001")))
    p = db.get(ActionProposal, run.outcome["proposal_id"])
    r = staff_client.post(f"/staff/proposals/{p.id}/approve", headers=as_reviewer(),
                          json={"payload_hash": p.payload_hash})
    assert r.status_code == 202

    with pytest.raises(WorkerKilled):
        work(saver=fresh_saver(), payment=DiesAfterSending(service_client), worker_id="worker-a")
    db.rollback()
    assert len(refunds_at_provider(mock_admin, p.id)) == 1  # money moved, but we never recorded the result
    assert db.query(RefundOperation).filter_by(proposal_id=p.id).one().status == "submitted"

    [done] = work(saver=fresh_saver(), now=after_lease(), worker_id="worker-b")
    assert (done.kind, done.status, done.attempts) == ("resume", "succeeded", 2)
    assert db.query(RefundOperation).filter_by(proposal_id=p.id).one().status == "succeeded"
    assert len(refunds_at_provider(mock_admin, p.id)) == 1  # reconciled by idempotency key, not resent
    assert db.get(Ticket, run.ticket_id).status == "resolved"
    assert db.get(WorkflowRun, run.id).status == "completed"
    assert graph_next(db, run.thread_id) == ()


def test_job_that_kills_every_worker_is_buried(client, db, work):
    tid = open_ticket(client, "refund?", "ord_1001")
    run, job = start_run(db, tid, "staff:staff|priya")
    job.status, job.attempts, job.locked_by = "running", job.max_attempts, "worker-z"
    job.locked_until = queue.utcnow()
    db.commit()

    assert work(ScriptedLLM([]), now=after_lease()) == []  # buried at claim time, never run again
    dead = db.get(Job, job.id, populate_existing=True)
    assert (dead.status, dead.last_error) == ("dead", "worker died on every attempt")
    assert db.get(WorkflowRun, run.id).status == "failed"
    assert db.get(Ticket, tid).status == "escalated"
    assert events(db, run.id, "job.dead_lettered")[0]["attempts"] == 3
