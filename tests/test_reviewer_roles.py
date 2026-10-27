"""Only reviewers decide, and never on their own proposals (four-eyes)."""

from tests.agent_scripts import open_ticket
from tests.conftest import ASHA, STAFF, as_customer, as_reviewer, as_staff


def manual_proposal(client) -> dict:
    tid = open_ticket(client, "refund?", "ord_1001")
    return client.post(f"/staff/tickets/{tid}/proposals", headers=as_staff()).json()  # created by staff|priya


def test_staff_role_cannot_approve(staff_client):
    p = manual_proposal(staff_client)
    r = staff_client.post(f"/staff/proposals/{p['id']}/approve", headers=as_staff("staff|someone_else"),
                          json={"payload_hash": p["payload_hash"]})
    assert r.status_code == 403 and r.json() == {"detail": "Reviewer role required"}


def test_customer_cannot_approve(staff_client):
    p = manual_proposal(staff_client)
    r = staff_client.post(f"/staff/proposals/{p['id']}/approve", headers=as_customer(ASHA),
                          json={"payload_hash": p["payload_hash"]})
    assert r.status_code == 403


def test_proposer_cannot_approve_own_proposal_even_as_reviewer(staff_client):
    p = manual_proposal(staff_client)
    assert p["created_by"] == STAFF
    for path in ("approve", "reject"):
        r = staff_client.post(f"/staff/proposals/{p['id']}/{path}", headers=as_reviewer(STAFF),
                              json={"payload_hash": p["payload_hash"]})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "self_approval"


def test_a_different_reviewer_can_approve(staff_client):
    p = manual_proposal(staff_client)
    r = staff_client.post(f"/staff/proposals/{p['id']}/approve", headers=as_reviewer(),
                          json={"payload_hash": p["payload_hash"]})
    assert r.status_code == 200
    assert r.json()["approval"]["reviewer_id"] == "reviewer|ravi"
    assert r.json()["run_status"] is None  # manual proposal: no agent workflow to resume


def test_reviewer_can_read_staff_views(staff_client):
    assert staff_client.get("/staff/proposals", headers=as_reviewer()).status_code == 200
