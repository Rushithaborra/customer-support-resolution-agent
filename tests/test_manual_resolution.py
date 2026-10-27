"""Part 4 done-when: a refund ticket resolves end to end with no LLM, and the
customer is only ever told what the provider confirmed."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import ActionProposal, Approval, RefundOperation, Ticket
from app.integrations.deps import get_payment_client
from app.integrations.payment import PaymentClient
from app.integrations.results import Rejected
from app.main import app
from tests.conftest import ASHA, VIKRAM, as_customer, as_staff, inject

STAFF = as_staff()


# ---------------------------------------------------------------- helpers


def open_ticket(client, order_id: str, category: str = "refund_request", who: str = ASHA) -> str:
    t = client.post("/tickets", headers=as_customer(who),
                    json={"message": "Where is my refund?", "order_id": order_id})
    assert t.status_code == 201
    tid = t.json()["id"]
    assert client.patch(f"/staff/tickets/{tid}", headers=STAFF, json={"category": category}).status_code == 200
    return tid


def propose(client, tid: str) -> dict:
    r = client.post(f"/staff/tickets/{tid}/proposals", headers=STAFF)
    assert r.status_code == 201, r.json()
    return r.json()


def approve(client, proposal: dict) -> None:
    r = client.post(f"/staff/proposals/{proposal['id']}/approve", headers=STAFF,
                    json={"payload_hash": proposal["payload_hash"]})
    assert r.status_code == 200, r.json()


def execute(client, proposal_id: str):
    return client.post(f"/staff/proposals/{proposal_id}/execute", headers=STAFF)


def approved_proposal(client, order_id: str = "ord_1001") -> tuple[str, dict]:
    tid = open_ticket(client, order_id)
    p = propose(client, tid)
    approve(client, p)
    return tid, p


def provider_refunds(mock_admin, proposal_id: str) -> list[dict]:
    return mock_admin.get("/payment/refunds",
                          params={"idempotency_key": f"refund-{proposal_id}"}).json()["refunds"]


def customer_view(client, tid: str, who: str = ASHA) -> dict:
    return client.get(f"/tickets/{tid}", headers=as_customer(who)).json()


# ---------------------------------------------------------------- happy path


def test_refund_resolves_end_to_end(staff_client, mock_admin, db):
    client = staff_client
    tid = open_ticket(client, "ord_1001")

    a = client.get(f"/staff/tickets/{tid}/assessment", headers=STAFF).json()
    assert a["refund"] == {"eligible": True, "reason_code": "eligible", "refundable_minor": 249900}
    assert a["evidence"]["return"]["inspection_status"] == "passed"

    p = propose(client, tid)
    assert p["payload"]["amount_minor"] == 249900  # computed by code, not typed by staff
    assert customer_view(client, tid)["outcome"] == {
        "state": "awaiting_approval", "message": "Your refund request is awaiting approval."}

    approve(client, p)
    r = execute(client, p["id"])
    assert r.status_code == 200
    op = r.json()
    assert (op["status"], op["amount_minor"], op["idempotency_key"]) == ("succeeded", 249900, f"refund-{p['id']}")

    assert len(provider_refunds(mock_admin, p["id"])) == 1
    assert db.get(Ticket, tid).status == "resolved"
    assert db.get(ActionProposal, p["id"]).status == "executed"
    assert customer_view(client, tid)["outcome"] == {
        "state": "succeeded", "message": "Your refund of ₹2,499.00 has been completed by our payment provider."}
    approval = db.query(Approval).filter_by(proposal_id=p["id"]).one()
    assert approval.reviewer_id == "staff|priya"  # from the token, server-side


# ---------------------------------------------------------------- code says no


@pytest.mark.parametrize("order_id, who, reason", [
    ("ord_1002", ASHA, "return_not_received"),
    ("ord_2001", VIKRAM, "inspection_failed"),
    ("ord_1003", ASHA, "not_delivered"),
    ("ord_1004", ASHA, "no_return"),
])
def test_ineligible_orders_get_no_proposal(staff_client, db, order_id, who, reason):
    tid = open_ticket(staff_client, order_id, who=who)
    r = staff_client.post(f"/staff/tickets/{tid}/proposals", headers=STAFF)
    assert r.status_code == 422
    assert r.json() == {"detail": {"code": reason}}
    assert db.get(Ticket, tid).status == "open"


def test_held_amount_is_an_int_not_decimal(staff_client, db):
    # Regression: Postgres SUM(bigint) returns NUMERIC, which reached the hash as a Decimal.
    from app.services.facts import held_refund_minor
    _, p = approved_proposal(staff_client)
    execute(staff_client, p["id"])
    held = held_refund_minor(db, "ord_1001")
    assert type(held) is int and held == 249900


def test_no_second_refund_after_success(staff_client):
    _, p = approved_proposal(staff_client)
    assert execute(staff_client, p["id"]).json()["status"] == "succeeded"
    tid2 = open_ticket(staff_client, "ord_1001")
    r = staff_client.post(f"/staff/tickets/{tid2}/proposals", headers=STAFF)
    assert r.json() == {"detail": {"code": "already_refunded"}}


# ---------------------------------------------------------------- approval binding


def test_execute_without_approval_is_refused(staff_client, mock_admin):
    p = propose(staff_client, open_ticket(staff_client, "ord_1001"))
    r = execute(staff_client, p["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_approved"
    assert provider_refunds(mock_admin, p["id"]) == []


def test_approving_a_different_hash_is_refused(staff_client):
    p = propose(staff_client, open_ticket(staff_client, "ord_1001"))
    r = staff_client.post(f"/staff/proposals/{p['id']}/approve", headers=STAFF, json={"payload_hash": "0" * 64})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "payload_hash_mismatch"


def test_payload_edited_after_approval_is_refused(staff_client, mock_admin, db):
    _, p = approved_proposal(staff_client)
    stored = db.get(ActionProposal, p["id"])
    stored.payload = {**stored.payload, "amount_minor": 999900}  # tamper directly in the DB
    db.commit()
    r = execute(staff_client, p["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_does_not_match_payload"
    assert provider_refunds(mock_admin, p["id"]) == []


def test_expired_approval_is_refused(staff_client, mock_admin, db):
    _, p = approved_proposal(staff_client)
    approval = db.query(Approval).filter_by(proposal_id=p["id"]).one()
    approval.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    r = execute(staff_client, p["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_expired"
    assert db.get(ActionProposal, p["id"]).status == "expired"
    assert provider_refunds(mock_admin, p["id"]) == []


def test_new_proposal_supersedes_old_one(staff_client):
    tid = open_ticket(staff_client, "ord_1001")
    first = propose(staff_client, tid)
    propose(staff_client, tid)
    r = staff_client.post(f"/staff/proposals/{first['id']}/approve", headers=STAFF,
                          json={"payload_hash": first["payload_hash"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_pending"


def test_reject_reopens_ticket(staff_client, db):
    tid = open_ticket(staff_client, "ord_1001")
    p = propose(staff_client, tid)
    assert staff_client.post(f"/staff/proposals/{p['id']}/reject", headers=STAFF).json()["status"] == "rejected"
    assert db.get(Ticket, tid).status == "open"


# ---------------------------------------------------------------- exactly once


def test_execute_twice_refunds_once(staff_client, mock_admin, db):
    _, p = approved_proposal(staff_client)
    first, second = execute(staff_client, p["id"]).json(), execute(staff_client, p["id"]).json()
    assert first == second
    assert db.query(RefundOperation).filter_by(proposal_id=p["id"]).count() == 1
    assert len(provider_refunds(mock_admin, p["id"])) == 1


def test_timeout_after_apply_reconciles_to_one_refund(staff_client, mock_admin, db):
    _, p = approved_proposal(staff_client)
    inject(mock_admin, "timeout_after_apply", "payment.create_refund")
    op = execute(staff_client, p["id"]).json()
    assert op["status"] == "pending_reconciliation"  # not 'failed': money may have moved
    tid = p["payload"]["ticket_id"]
    assert "completed" not in customer_view(staff_client, tid)["outcome"]["message"]

    op = staff_client.post(f"/staff/refunds/{op['id']}/refresh", headers=STAFF).json()
    assert op["status"] == "succeeded"
    assert len(provider_refunds(mock_admin, p["id"])) == 1
    assert db.get(Ticket, tid).status == "resolved"


def test_timeout_before_apply_resends_with_same_key(staff_client, mock_admin):
    _, p = approved_proposal(staff_client)
    inject(mock_admin, "timeout_before_apply", "payment.create_refund")
    op = execute(staff_client, p["id"]).json()
    assert op["status"] == "pending_reconciliation"
    assert provider_refunds(mock_admin, p["id"]) == []  # it really didn't happen

    op = staff_client.post(f"/staff/refunds/{op['id']}/refresh", headers=STAFF).json()
    assert op["status"] == "succeeded"
    assert len(provider_refunds(mock_admin, p["id"])) == 1


def test_provider_503_then_retry(staff_client, mock_admin, db):
    _, p = approved_proposal(staff_client)
    inject(mock_admin, "error_503", "payment.create_refund")
    r = execute(staff_client, p["id"])
    assert r.status_code == 503
    assert db.query(RefundOperation).filter_by(proposal_id=p["id"]).one().status == "submitted"
    assert execute(staff_client, p["id"]).json()["status"] == "succeeded"
    assert len(provider_refunds(mock_admin, p["id"])) == 1


def test_processing_then_settled(staff_client, mock_admin, db):
    mock_admin.post("/_admin/payment/config", json={"new_refund_status": "processing"}).raise_for_status()
    tid, p = approved_proposal(staff_client)
    op = execute(staff_client, p["id"]).json()
    assert op["status"] == "processing"
    assert db.get(Ticket, tid).status == "awaiting_provider"
    assert customer_view(staff_client, tid)["outcome"]["state"] == "processing"

    mock_admin.post(f"/_admin/payment/refunds/{op['provider_reference']}/settle",
                    json={"status": "succeeded"}).raise_for_status()
    op = staff_client.post(f"/staff/refunds/{op['id']}/refresh", headers=STAFF).json()
    assert op["status"] == "succeeded"
    assert db.get(Ticket, tid).status == "resolved"


def test_provider_rejection_fails_and_releases_balance(staff_client, db):
    class RejectingPayment(PaymentClient):
        def __init__(self):
            pass

        def create_refund(self, **_):
            return Rejected(422, "card_closed", "Destination closed")

    app.dependency_overrides[get_payment_client] = RejectingPayment
    tid, p = approved_proposal(staff_client)
    assert execute(staff_client, p["id"]).json()["status"] == "failed"
    assert db.get(Ticket, tid).status == "escalated"
    del app.dependency_overrides[get_payment_client]
    # The failed operation no longer holds the balance, so a fresh proposal is allowed.
    assert propose(staff_client, open_ticket(staff_client, "ord_1001"))["payload"]["amount_minor"] == 249900


def test_no_transaction_open_during_provider_call(staff_client, service_client, db):
    seen: list[bool] = []

    class SpyPayment(PaymentClient):
        def create_refund(self, **kwargs):
            seen.append(db.in_transaction())
            return super().create_refund(**kwargs)

    app.dependency_overrides[get_payment_client] = lambda: SpyPayment(service_client)
    _, p = approved_proposal(staff_client)
    assert execute(staff_client, p["id"]).json()["status"] == "succeeded"
    assert seen == [False]


# ---------------------------------------------------------------- non-money outcomes


def test_order_status_uses_shipping(staff_client):
    tid = open_ticket(staff_client, "ord_1003", category="order_status")
    a = staff_client.get(f"/staff/tickets/{tid}/assessment", headers=STAFF).json()
    assert a["shipment"]["status"] == "in_transit" and a["shipment"]["eta"] is not None
    r = staff_client.post(f"/staff/tickets/{tid}/close", headers=STAFF,
                          json={"outcome": "resolved", "summary": "Told customer the ETA."})
    assert r.json()["status"] == "resolved"


def test_order_status_when_shipping_is_down(staff_client, mock_admin):
    tid = open_ticket(staff_client, "ord_1003", category="order_status")
    inject(mock_admin, "error_503", "shipping.get_shipment")
    a = staff_client.get(f"/staff/tickets/{tid}/assessment", headers=STAFF).json()
    assert (a["shipment"], a["shipment_error"]) == (None, "shipping_unavailable")


def test_damage_claim_escalates(staff_client):
    tid = open_ticket(staff_client, "ord_1004", category="damaged_item")
    a = staff_client.get(f"/staff/tickets/{tid}/assessment", headers=STAFF).json()
    assert a["damage"] == {"recommendation": "escalate", "reason_code": "damage_report_in_window"}
    staff_client.post(f"/staff/tickets/{tid}/close", headers=STAFF,
                      json={"outcome": "escalated", "summary": "Needs photo review."})
    assert customer_view(staff_client, tid)["outcome"]["state"] == "escalated"


def test_cannot_close_ticket_with_refund_in_flight(staff_client):
    tid, _ = approved_proposal(staff_client)
    r = staff_client.post(f"/staff/tickets/{tid}/close", headers=STAFF,
                          json={"outcome": "resolved", "summary": "done"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "refund_in_flight"
