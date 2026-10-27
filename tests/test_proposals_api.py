"""The reviewer queue."""

from tests.conftest import ASHA, as_customer, as_staff
from tests.agent_scripts import draft, open_ticket, refund_script

STAFF = as_staff()


def test_pending_queue_lists_agent_proposal(agent, client):
    tid = open_ticket(client, "refund?", "ord_1001")
    run = agent(tid, refund_script(draft("ord_1001")))
    queue = client.get("/staff/proposals", params={"status": "pending_approval"}, headers=STAFF).json()
    [item] = [p for p in queue if p["id"] == run.outcome["proposal_id"]]
    assert item["payload"]["proposed_by"] == "agent"
    assert item["payload"]["evidence_refs"]
    assert len(item["payload_hash"]) == 64


def test_queue_filters_by_status(agent, client):
    tid = open_ticket(client, "refund?", "ord_1001")
    run = agent(tid, refund_script(draft("ord_1001")))
    executed = client.get("/staff/proposals", params={"status": "executed"}, headers=STAFF).json()
    assert run.outcome["proposal_id"] not in [p["id"] for p in executed]
    assert client.get("/staff/proposals", params={"status": "bogus"}, headers=STAFF).status_code == 422


def test_queue_is_staff_only(client):
    assert client.get("/staff/proposals", headers=as_customer(ASHA)).status_code == 403
