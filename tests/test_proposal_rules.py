"""Code-side checks on an agent's refund draft. Pure: no DB, no LLM."""

import pytest

from app.domain.eligibility import RefundDecision
from app.domain.proposal_rules import validate_refund_draft

ELIGIBLE = RefundDecision(True, "eligible", 249900)
TRACE = [
    {"tool": "check_refund_eligibility", "input": {"order_id": "ord_1001"}, "ok": True, "code": None},
    {"tool": "get_return_status", "input": {"order_id": "ord_1001"}, "ok": True, "code": None},
    {"tool": "get_shipment", "input": {"order_id": "ord_1001"}, "ok": False, "code": "unavailable"},
    {"tool": "list_my_orders", "input": {}, "ok": True, "code": None},
]
GOOD = {"order_id": "ord_1001", "amount_minor": 249900, "currency": "INR",
        "evidence_refs": ["check_refund_eligibility:ord_1001", "get_return_status:ord_1001"]}


def codes(draft: dict, decision: RefundDecision = ELIGIBLE, trace=TRACE) -> list[str]:
    return [v.code for v in validate_refund_draft(draft, resolved_order_id="ord_1001", decision=decision,
                                                  currency="INR", trace=trace)]


def test_valid_draft_has_no_violations():
    assert codes(GOOD) == []
    assert codes({**GOOD, "evidence_refs": [*GOOD["evidence_refs"], "list_my_orders"]}) == []


@pytest.mark.parametrize("amount", [249899, 249901, 0, -249900, 2499000, 999900])
def test_any_other_amount_is_rejected(amount):
    assert codes({**GOOD, "amount_minor": amount}) == ["amount_mismatch"]


@pytest.mark.parametrize("ref", [
    "get_refund_status:ord_1001",   # never called
    "get_shipment:ord_1001",        # called, but it failed
    "get_return_status:ord_1002",   # called for a different order
])
def test_evidence_must_be_a_successful_call_in_this_run(ref):
    assert codes({**GOOD, "evidence_refs": [*GOOD["evidence_refs"], ref]}) == ["unsupported_evidence"]


def test_code_eligibility_must_be_cited():
    assert codes({**GOOD, "evidence_refs": ["get_return_status:ord_1001"]}) == ["missing_required_evidence"]


def test_wrong_order_and_currency():
    assert "wrong_order" in codes({**GOOD, "order_id": "ord_1002"})
    assert codes({**GOOD, "currency": "USD"}) == ["wrong_currency"]


def test_not_eligible_is_a_violation_even_if_draft_looks_fine():
    assert "not_eligible" in codes(GOOD, decision=RefundDecision(False, "already_refunded", 0))


def test_all_violations_are_reported_together():
    bad = {"order_id": "ord_1002", "amount_minor": 1, "currency": "USD", "evidence_refs": ["get_order:ord_9999"]}
    assert set(codes(bad)) == {"wrong_order", "wrong_currency", "amount_mismatch", "unsupported_evidence",
                               "missing_required_evidence"}
