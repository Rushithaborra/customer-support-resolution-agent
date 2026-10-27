"""The refund rules as pure functions: every reason code, every boundary."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.customer_messages import customer_outcome, format_inr
from app.domain.eligibility import OrderFacts, ReturnFacts, assess_damage, evaluate_refund
from app.domain.hashing import payload_hash

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
DELIVERED = NOW - timedelta(days=12)

RECEIVED_PASSED = ReturnFacts("ret_x", "received", "passed", NOW - timedelta(days=3))
ELIGIBLE = OrderFacts("ord_x", "delivered", "INR", 249900, DELIVERED, RECEIVED_PASSED, held_minor=0)


def _with_return(**changes) -> OrderFacts:
    return replace(ELIGIBLE, latest_return=replace(RECEIVED_PASSED, **changes))


def test_eligible_refund_is_full_paid_amount():
    d = evaluate_refund(ELIGIBLE)
    assert (d.eligible, d.reason_code, d.refundable_minor) == (True, "eligible", 249900)


@pytest.mark.parametrize("facts, reason", [
    (replace(ELIGIBLE, status="shipped", delivered_at=None), "not_delivered"),
    (replace(ELIGIBLE, latest_return=None), "no_return"),
    (_with_return(status="in_transit", received_at=None), "return_not_received"),
    (_with_return(status="requested", received_at=None), "return_not_received"),
    (_with_return(status="rejected"), "return_rejected"),
    (_with_return(inspection_status="pending"), "inspection_pending"),
    (_with_return(inspection_status="failed"), "inspection_failed"),
    (replace(ELIGIBLE, held_minor=249900), "already_refunded"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_each_rule_blocks_with_its_reason(facts, reason):
    d = evaluate_refund(facts)
    assert (d.eligible, d.reason_code, d.refundable_minor) == (False, reason, 0)


def test_return_window_boundary():
    on_day_30 = _with_return(received_at=DELIVERED + timedelta(days=30))
    day_31 = _with_return(received_at=DELIVERED + timedelta(days=31))
    assert evaluate_refund(on_day_30).eligible
    assert evaluate_refund(day_31).reason_code == "outside_return_window"


def test_partial_hold_reduces_refundable():
    d = evaluate_refund(replace(ELIGIBLE, held_minor=100000))
    assert d.eligible and d.refundable_minor == 149900


def test_damage_window():
    assert assess_damage(replace(ELIGIBLE, delivered_at=NOW - timedelta(days=2)), NOW).recommendation == "escalate"
    late = assess_damage(replace(ELIGIBLE, delivered_at=NOW - timedelta(days=8)), NOW)
    assert (late.recommendation, late.reason_code) == ("not_eligible", "outside_damage_window")


def test_payload_hash_ignores_key_order_but_not_values():
    a = {"amount_minor": 249900, "order_id": "ord_1001", "evidence": {"x": 1, "y": 2}}
    b = {"evidence": {"y": 2, "x": 1}, "order_id": "ord_1001", "amount_minor": 249900}
    assert payload_hash(a) == payload_hash(b)
    assert payload_hash(a) != payload_hash({**a, "amount_minor": 249901})


def test_customer_text_never_claims_completion_early():
    for status in ["reserved", "submitted", "processing", "pending_reconciliation", "failed"]:
        assert "completed" not in customer_outcome("awaiting_provider", status, 249900).message
    done = customer_outcome("resolved", "succeeded", 249900)
    assert done.message == "Your refund of ₹2,499.00 has been completed by our payment provider."
    assert format_inr(5) == "₹0.05"
