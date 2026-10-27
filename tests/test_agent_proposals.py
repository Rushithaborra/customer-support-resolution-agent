"""Part 7 done-when: agent proposals carry evidence refs, and wrong amounts are rejected by code."""

import pytest
from sqlalchemy import select

from app.db.models import ActionProposal, Ticket
from app.llm.fake import ScriptedLLM, text_turn, tool_turn
from tests.conftest import ASHA, as_customer, as_staff
from tests.agent_scripts import draft, open_ticket, refund_script, summary, triage

STAFF = as_staff()


def proposals_for(db, run_id: str) -> list[ActionProposal]:
    return list(db.scalars(select(ActionProposal).where(ActionProposal.run_id == run_id)).all())


@pytest.fixture
def ticket(client) -> str:
    return open_ticket(client, "I returned my headphones last week but haven't got my refund.", "ord_1001")


# ---------------------------------------------------------------- accepted


def test_valid_draft_becomes_a_pending_proposal_with_evidence(agent, ticket, db, client):
    refs = ["check_refund_eligibility:ord_1001", "get_return_status:ord_1001"]
    run = agent(ticket, refund_script(draft("ord_1001", 249900, refs)))

    assert (run.route, run.status) == ("propose_refund", "paused")
    [p] = proposals_for(db, run.id)
    assert p.status == "pending_approval" and run.outcome["proposal_id"] == p.id
    assert p.payload["evidence_refs"] == refs
    assert (p.payload["proposed_by"], p.payload["amount_minor"]) == ("agent", 249900)
    assert p.payload["rationale"] == "Return received and inspection passed."
    assert db.get(Ticket, ticket).status == "awaiting_approval"
    assert client.get(f"/tickets/{ticket}", headers=as_customer(ASHA)).json()["outcome"]["message"] == \
        "Your refund request is awaiting approval."


# ---------------------------------------------------------------- rejected by code


def test_wrong_amount_is_rejected_and_escalated(agent, ticket, db):
    run = agent(ticket, refund_script(draft("ord_1001", 2499000)))  # ₹24,990 instead of ₹2,499
    assert (run.route, run.route_reason, run.status) == ("escalate", "proposal_rejected:amount_mismatch", "escalated")
    assert proposals_for(db, run.id) == []
    assert run.outcome["proposal_violations"] == [
        {"code": "amount_mismatch", "detail": "draft 2499000 != refundable 249900"}]
    assert run.outcome["proposal_draft"]["amount_minor"] == 2499000  # kept for review and the eval
    assert db.get(Ticket, ticket).status == "escalated"


def test_citing_a_tool_call_that_never_happened_is_rejected(agent, ticket, db):
    llm = refund_script(draft("ord_1001", refs=["check_refund_eligibility:ord_1001", "get_return_status:ord_1001"]),
                 tools=(("check_refund_eligibility", "ord_1001"),))  # get_return_status never called
    run = agent(ticket, llm)
    assert run.route_reason == "proposal_rejected:unsupported_evidence"
    assert proposals_for(db, run.id) == []


def test_draft_for_another_order_is_rejected(agent, ticket):
    run = agent(ticket, refund_script(draft("ord_1002", refs=["check_refund_eligibility:ord_1002"])))
    assert run.route_reason == "proposal_rejected:wrong_order"
    assert {v["code"] for v in run.outcome["proposal_violations"]} >= {"wrong_order", "unsupported_evidence"}


def test_draft_that_fails_validation_twice_escalates(agent, ticket, db):
    junk = text_turn("I'd suggest refunding them")
    llm = refund_script(junk)
    llm._turns.append(junk)
    run = agent(ticket, llm)
    assert run.route_reason == "proposal_draft_failed:invalid_output"
    assert proposals_for(db, run.id) == []


def test_non_refund_routes_create_no_proposal(agent, client, db):
    tid = open_ticket(client, "refund please", "ord_1002")
    run = agent(tid, ScriptedLLM([triage("refund_request", "ord_1002"),
                                  tool_turn(("check_refund_eligibility", {"order_id": "ord_1002"})),
                                  text_turn("not eligible"),
                                  summary("explain_policy", "check_refund_eligibility:ord_1002")]))
    assert run.route == "explain_policy" and proposals_for(db, run.id) == []


# ---------------------------------------------------------------- the full path


def test_agent_proposal_approved_and_executed_once(agent, ticket, staff_client, mock_admin, db):
    run = agent(ticket, refund_script(draft("ord_1001")))
    p = staff_client.get(f"/staff/proposals/{run.outcome['proposal_id']}", headers=STAFF).json()

    assert staff_client.post(f"/staff/proposals/{p['id']}/approve", headers=STAFF,
                             json={"payload_hash": p["payload_hash"]}).status_code == 200
    op = staff_client.post(f"/staff/proposals/{p['id']}/execute", headers=STAFF).json()
    assert (op["status"], op["amount_minor"]) == ("succeeded", 249900)
    refunds = mock_admin.get("/payment/refunds", params={"idempotency_key": f"refund-{p['id']}"}).json()["refunds"]
    assert len(refunds) == 1
    assert db.get(Ticket, ticket).status == "resolved"


def test_editing_evidence_after_approval_invalidates_it(agent, ticket, staff_client, mock_admin, db):
    run = agent(ticket, refund_script(draft("ord_1001")))
    p = db.get(ActionProposal, run.outcome["proposal_id"])
    staff_client.post(f"/staff/proposals/{p.id}/approve", headers=STAFF, json={"payload_hash": p.payload_hash})
    p.payload = {**p.payload, "evidence_refs": ["get_order:ord_1001"]}
    db.commit()
    r = staff_client.post(f"/staff/proposals/{p.id}/execute", headers=STAFF)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_does_not_match_payload"
    assert mock_admin.get("/payment/refunds", params={"idempotency_key": f"refund-{p.id}"}).json()["refunds"] == []


def test_staff_proposals_carry_code_evidence_refs(staff_client):
    tid = open_ticket(staff_client, "refund?", "ord_1001")
    staff_client.patch(f"/staff/tickets/{tid}", headers=STAFF, json={"category": "refund_request"})
    p = staff_client.post(f"/staff/tickets/{tid}/proposals", headers=STAFF).json()
    assert p["payload"]["evidence_refs"] == ["code:evaluate_refund:ord_1001", "db:return:ret_1001"]
    assert p["payload"]["proposed_by"] == "staff:staff|priya"
