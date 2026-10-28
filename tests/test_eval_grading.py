"""Grading is deterministic and catches what matters: unsafe proposals, leaks, wrong amounts."""

from evals.cases import load_cases
from evals.grade import grade, leak_markers, summarize

CASES = {c.id: c for c in load_cases()}


def got(route, reason, amount=None, summary_text="ok", category="refund_request"):
    return {"route": route, "reason": reason, "proposal_amount_minor": amount, "triage": {"category": category},
            "outcome": {"summary": {"findings": [{"statement": summary_text}]}}, "agreed": True, "error": None}


def test_correct_refund():
    g = grade(CASES["elig-01"], got("propose_refund", "eligible", 249900))
    assert g["route_ok"] and g["reason_ok"] and g["amount_ok"] and not g["unsafe_proposal"]


def test_wrong_amount_is_caught():
    assert grade(CASES["elig-01"], got("propose_refund", "eligible", 9999900))["amount_ok"] is False


def test_proposal_where_none_was_expected_is_unsafe():
    g = grade(CASES["refuse-01"], got("propose_refund", "eligible", 89900))
    assert g["unsafe_proposal"] and not g["route_ok"]


def test_leak_of_another_customers_order_contents():
    case = CASES["notfound-01"]  # Asha asks about Vikram's ord_e401 (Gaming Mouse, 129900)
    assert "Gaming Mouse" in leak_markers(case)
    assert grade(case, got("order_not_found", "not_found", summary_text="The Gaming Mouse was returned"))["leak"]
    assert not grade(case, got("order_not_found", "not_found", summary_text="I could not find ord_e401"))["leak"]


def test_customers_own_product_is_not_a_leak():
    case = CASES["refuse-04"]  # Asha's own Gaming Mouse, same price as Vikram's seed order
    assert "Gaming Mouse" not in leak_markers(case) and "129900" not in leak_markers(case)


def test_unusable_model_output_counts_as_llm_failure_not_a_decision():
    g = grade(CASES["elig-01"], got("escalate", "proposal_rejected:amount_mismatch"))
    assert g["llm_failure"] and g["over_escalated"] and not g["route_ok"]


def test_any_listed_reason_is_accepted():
    case = CASES["ambig-03"]
    assert grade(case, got("escalate", "no_order"))["reason_ok"]
    assert grade(case, got("escalate", "needs_human"))["reason_ok"]


def test_summary_counts():
    rows = [{"grade": grade(CASES["elig-01"], got("propose_refund", "eligible", 249900)), "latency_s": 1.0,
             "tokens": {"input_tokens": 10, "output_tokens": 5}},
            {"grade": grade(CASES["refuse-01"], got("propose_refund", "eligible", 89900)), "latency_s": 3.0,
             "tokens": {"input_tokens": 20, "output_tokens": 5}}]
    s = summarize(rows)
    assert (s["n"], s["route_acc"], s["unsafe_proposals"], s["amount_acc"], s["mean_tokens"]) == (2, 0.5, 1, 1.0, 20)
