"""Part 11 done-when: timeout, restart and concurrent tests produce zero duplicate refunds,
and no refund is left in limbo.

The matrix drives the real path: an approved agent proposal, the worker's resume job
(with a fault at one point), then the worker's reconcile sweep with the clock moved
past the grace window. At every step the provider holds at most one refund; at the end,
exactly one, and the ticket is resolved.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.auth import REVIEWER_ROLE, Staff
from app.db.models import ActionProposal, Approval, AuditEvent, Job, RefundOperation, Ticket, WorkflowRun
from app.db.session import SessionLocal
from app.integrations.http import ServiceClient
from app.integrations.payment import PaymentClient, ProviderRefund
from app.integrations.results import Ok
from app.jobs import queue
from app.policy.embedder import get_embedder
from app.services import approvals, refunds, resolution
from app.services.errors import NotEligible
from tests.agent_scripts import draft, open_ticket, refund_script
from tests.conftest import ASHA, CLIENT_TIMEOUT_S, as_customer, as_reviewer, as_staff, inject
from tests.test_approval_restart import refunds_at_provider


class WorkerKilled(BaseException):
    """Stands in for kill -9: escapes every `except Exception`."""


class DiesBeforeSending(PaymentClient):
    def create_refund(self, **kw):
        raise WorkerKilled()


class DiesAfterSending(PaymentClient):
    def create_refund(self, **kw):
        super().create_refund(**kw)
        raise WorkerKilled()


def past_grace() -> datetime:
    return datetime.now(timezone.utc) + refunds.RECONCILE_GRACE + timedelta(seconds=1)


def customer_message(client, tid: str) -> str:
    return client.get(f"/tickets/{tid}", headers=as_customer(ASHA)).json()["outcome"]["message"]


@pytest.fixture
def approved(agent, client, staff_client, db):
    """An agent run for ord_1001, approved by a reviewer; its resume job is queued, not yet run."""
    run = agent(open_ticket(client, "I returned my headphones, where's my refund?", "ord_1001"),
                refund_script(draft("ord_1001")))
    p = db.get(ActionProposal, run.outcome["proposal_id"])
    r = staff_client.post(f"/staff/proposals/{p.id}/approve", headers=as_reviewer(),
                          json={"payload_hash": p.payload_hash})
    assert r.status_code == 202
    return run, p


def sweep(db, work, now):
    queue.enqueue(db, "reconcile_sweep", now=now)
    db.commit()
    return work(now=now)


# ---------------------------------------------------------------- the failure-point matrix


def _no_fault(admin, http):
    return None


def _fault(kind):
    def setup(admin, http):
        inject(admin, kind, "payment.create_refund", delay_s=CLIENT_TIMEOUT_S * 3)
    return setup


def _payment(cls):
    return lambda admin, http: cls(http)


def _processing(admin, http):
    admin.post("/_admin/payment/config", json={"new_refund_status": "processing"}).raise_for_status()


FAULTS = {
    "provider_503": _fault("error_503"),
    "timeout_before_apply": _fault("timeout_before_apply"),
    "timeout_after_apply": _fault("timeout_after_apply"),
    "500_on_write": _fault("error_500"),
    "slower_than_client_timeout": _fault("slow"),
    "killed_before_send": _payment(DiesBeforeSending),
    "killed_after_provider_accepted": _payment(DiesAfterSending),
    "processing_then_settled": _processing,
}


@pytest.mark.parametrize("fault", FAULTS)
def test_every_failure_point_ends_in_exactly_one_refund(fault, approved, client, db, work, mock_admin,
                                                        service_client):
    run, p = approved
    payment = FAULTS[fault](mock_admin, service_client)

    try:
        work(payment=payment)  # the resume job: approval → execute_refund, with the fault
    except WorkerKilled:
        db.rollback()  # the process died; its open transaction is gone
    op = db.scalars(select(RefundOperation).where(RefundOperation.proposal_id == p.id)).one()
    assert op.status != "failed"  # unknown is never failed
    assert len(refunds_at_provider(mock_admin, p.id)) <= 1
    if op.status != "succeeded":
        assert "completed" not in customer_message(client, run.ticket_id)  # rule 8

    if fault == "processing_then_settled":
        assert op.status == "processing"
        mock_admin.post(f"/_admin/payment/refunds/{op.provider_reference}/settle",
                        json={"status": "succeeded"}).raise_for_status()
    if fault == "slower_than_client_timeout":
        time.sleep(CLIENT_TIMEOUT_S * 3)  # the provider finishes the slow request it was still applying

    sweep(db, work, past_grace())
    db.refresh(op)
    assert len(refunds_at_provider(mock_admin, p.id)) == 1
    assert db.query(RefundOperation).filter_by(proposal_id=p.id).count() == 1
    assert (op.status, op.next_reconcile_at, op.needs_review_reason) == ("succeeded", None, None)
    assert db.get(Ticket, run.ticket_id).status == "resolved"
    assert db.get(WorkflowRun, run.id).status == "completed"
    assert "completed by our payment provider" in customer_message(client, run.ticket_id)


# ---------------------------------------------------------------- limbo and edges


def test_sweep_waits_out_the_grace_window_then_backs_off(approved, db, work, mock_admin, service_client):
    run, p = approved
    inject(mock_admin, "error_503", "payment.create_refund")
    work()
    op = db.scalars(select(RefundOperation).where(RefundOperation.proposal_id == p.id)).one()
    assert refunds.reconcile_due(db, None, datetime.now(timezone.utc)) == []  # sender may still be mid-call

    inject(mock_admin, "error_503", "payment.find_refunds")  # the provider still can't answer
    t = past_grace()
    assert refunds.reconcile_due(db, PaymentClient(service_client), t) == [op.id]
    db.refresh(op)
    assert (op.status, op.reconcile_attempts, op.next_reconcile_at - t) == ("submitted", 1, timedelta(seconds=60))
    assert refunds.reconcile_due(db, PaymentClient(service_client), t + timedelta(seconds=59)) == []


def test_provider_that_never_answers_parks_the_refund_for_a_human(approved, db, work, mock_admin, service_client):
    run, p = approved
    inject(mock_admin, "timeout_after_apply", "payment.create_refund")
    work()
    op = db.scalars(select(RefundOperation).where(RefundOperation.proposal_id == p.id)).one()
    op.reconcile_attempts, op.next_reconcile_at = refunds.MAX_RECONCILE_ATTEMPTS, datetime.now(timezone.utc)
    db.commit()

    assert refunds.reconcile_due(db, PaymentClient(service_client), past_grace()) == []
    db.refresh(op)
    assert (op.status, op.needs_review_reason, op.next_reconcile_at) == \
        ("pending_reconciliation", "reconcile_exhausted", None)  # still unknown, NOT failed
    assert db.get(Ticket, run.ticket_id).status == "escalated"
    op.next_reconcile_at = datetime.now(timezone.utc)
    db.commit()
    assert refunds.reconcile_due(db, PaymentClient(service_client), past_grace()) == []  # left alone

    # A human checks and refreshes: the provider did apply it, so it settles and unparks.
    settled = refunds.refresh(db, "staff:staff|priya", op.id, PaymentClient(service_client))
    assert (settled.status, settled.needs_review_reason) == ("succeeded", None)
    assert len(refunds_at_provider(mock_admin, p.id)) == 1


def test_provider_record_that_disagrees_is_parked_not_trusted(approved, db, work, mock_admin, service_client):
    run, p = approved
    inject(mock_admin, "timeout_before_apply", "payment.create_refund")
    work()

    class Disagreeing(PaymentClient):
        def find_refund_by_key(self, key):
            return Ok(ProviderRefund(refund_id="re_x", order_id="ord_1001", amount_minor=1, currency="INR",
                                     status="succeeded", idempotency_key=key, created_at=datetime.now(timezone.utc)))

    refunds.reconcile_due(db, Disagreeing(service_client), past_grace())
    op = db.scalars(select(RefundOperation).where(RefundOperation.proposal_id == p.id)).one()
    assert (op.status, op.needs_review_reason) == ("pending_reconciliation", "provider_mismatch")
    assert db.get(Ticket, run.ticket_id).status == "escalated"
    assert refunds_at_provider(mock_admin, p.id) == []  # nothing resent on a disagreement


def test_never_sent_refund_with_expired_approval_is_cancelled_and_frees_the_balance(staff_client, db,
                                                                                   service_client):
    tid = open_ticket(staff_client, "refund?", "ord_1001")
    p = staff_client.post(f"/staff/tickets/{tid}/proposals", headers=as_staff()).json()
    staff_client.post(f"/staff/proposals/{p['id']}/approve", headers=as_reviewer(),
                      json={"payload_hash": p["payload_hash"]})
    op = refunds._reserve(db, "staff:staff|priya", db.get(ActionProposal, p["id"]))  # then the process died
    db.scalars(select(Approval).where(Approval.proposal_id == p["id"])).one().expires_at = \
        datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    refunds.reconcile_due(db, PaymentClient(service_client), past_grace())
    db.refresh(op)
    assert (op.status, op.next_reconcile_at) == ("failed", None)  # accurate: it never reached the provider
    assert db.get(Ticket, tid).status == "escalated"
    fresh = staff_client.post(f"/staff/tickets/{open_ticket(staff_client, 'again', 'ord_1001')}/proposals",
                              headers=as_staff())
    assert fresh.json()["payload"]["amount_minor"] == 249900  # the held balance was released


def test_unsettled_refunds_list_is_staff_only(approved, client, db, work, mock_admin):
    _, p = approved
    inject(mock_admin, "timeout_after_apply", "payment.create_refund")
    work()
    listed = client.get("/staff/refunds", headers=as_staff()).json()
    assert [r["proposal_id"] for r in listed] == [p.id]
    assert client.get("/staff/refunds?state=needs_review", headers=as_staff()).json() == []
    assert client.get("/staff/refunds", headers=as_customer(ASHA)).status_code == 403


# ---------------------------------------------------------------- real concurrency (separate connections)


@pytest.fixture
def committed(mock_server_url, mock_admin):
    """Approved manual proposals committed for real, so parallel sessions can see them. Cleaned up after."""
    tickets: list[str] = []

    def make(order_id: str = "ord_1001") -> ActionProposal:
        with SessionLocal() as s:
            t = Ticket(customer_id="cus_asha", order_id=order_id, customer_message="concurrency test")
            s.add(t)
            s.commit()
            tickets.append(t.id)
            p = resolution.propose_refund(s, Staff("staff|priya"), t.id, get_embedder())
            approvals.approve(s, Staff("reviewer|ravi", REVIEWER_ROLE), p.id, p.payload_hash,
                              datetime.now(timezone.utc))
            return p
    yield make
    with SessionLocal() as s:
        runs = s.scalars(select(WorkflowRun.id).where(WorkflowRun.ticket_id.in_(tickets))).all()
        props = s.scalars(select(ActionProposal.id).where(ActionProposal.run_id.in_(runs))).all()
        s.execute(delete(AuditEvent).where(AuditEvent.run_id.in_(runs)))
        s.execute(delete(Job).where(Job.run_id.in_(runs)))
        s.execute(delete(RefundOperation).where(RefundOperation.proposal_id.in_(props)))
        s.execute(delete(Approval).where(Approval.proposal_id.in_(props)))
        s.execute(delete(ActionProposal).where(ActionProposal.id.in_(props)))
        s.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(runs)))
        s.execute(delete(Ticket).where(Ticket.id.in_(tickets)))
        s.commit()


def in_parallel(mock_server_url, *calls):
    """Run each call(session, payment) on its own thread, connection and HTTP client, all released together."""
    barrier, results = threading.Barrier(len(calls)), [None] * len(calls)

    def runner(i, fn):
        with SessionLocal() as s, ServiceClient(base_url=mock_server_url, connect_timeout_s=2,
                                                read_timeout_s=2) as http:
            barrier.wait()
            try:
                results[i] = fn(s, PaymentClient(http))
            except Exception as e:  # noqa: BLE001 - the test inspects what each racer got
                results[i] = e
    threads = [threading.Thread(target=runner, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_two_executes_of_one_proposal_at_once_refund_once(committed, mock_server_url, mock_admin):
    p = committed()
    go = lambda s, pay: refunds.execute(s, "staff:staff|priya", p.id, pay).status  # noqa: E731
    results = in_parallel(mock_server_url, go, go, go)
    assert len(refunds_at_provider(mock_admin, p.id)) == 1
    assert "succeeded" in results
    with SessionLocal() as s:
        assert s.query(RefundOperation).filter_by(proposal_id=p.id).count() == 1


def test_two_proposals_for_one_order_cannot_refund_it_twice(committed, mock_server_url, mock_admin):
    """Two tickets about the same order, both approved. FOR UPDATE on the order serialises the
    reservations, so the second sees the first's hold and the order is refunded once."""
    a, b = committed(), committed()
    results = in_parallel(mock_server_url,
                          lambda s, pay: refunds.execute(s, "staff:x", a.id, pay).status,
                          lambda s, pay: refunds.execute(s, "staff:x", b.id, pay).status)
    [loser] = [r for r in results if r != "succeeded"]
    assert isinstance(loser, NotEligible) and loser.reason_code in ("already_refunded", "exceeds_refundable")
    total = len(refunds_at_provider(mock_admin, a.id)) + len(refunds_at_provider(mock_admin, b.id))
    assert total == 1


def test_sweep_racing_a_slow_send_still_refunds_once(committed, mock_server_url, mock_admin):
    """Worst case: the sweep ignores the grace window and asks while the send is still in flight.
    It finds nothing and resends with the SAME key; the provider dedupes it."""
    p = committed()
    inject(mock_admin, "slow", "payment.create_refund", delay_s=1.0)

    def send(s, pay):
        return refunds.execute(s, "staff:x", p.id, pay).status

    def sweep_early(s, pay):
        time.sleep(0.3)  # the send is mid-call now
        return refunds.reconcile_due(s, pay, past_grace())

    in_parallel(mock_server_url, send, sweep_early)
    with SessionLocal() as s, ServiceClient(base_url=mock_server_url) as http:
        refunds.reconcile_due(s, PaymentClient(http), past_grace() + timedelta(hours=1))
        op = s.scalars(select(RefundOperation).where(RefundOperation.proposal_id == p.id)).one()
        assert op.status == "succeeded"
    assert len(refunds_at_provider(mock_admin, p.id)) == 1
