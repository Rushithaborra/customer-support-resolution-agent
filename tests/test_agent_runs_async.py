"""The async run API: 202 + Location, one active run per ticket, and the run's event timeline."""

import json

from tests.agent_scripts import full_script, open_ticket
from tests.conftest import ASHA, as_customer, as_staff

MESSAGE = "I returned my headphones, where's my refund? My phone is 98450 12345."


def start(client, tid: str):
    return client.post(f"/staff/tickets/{tid}/agent-runs", headers=as_staff())


def test_second_start_while_queued_is_409(client):
    tid = open_ticket(client, MESSAGE, "ord_1001")
    assert start(client, tid).status_code == 202
    r = start(client, tid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "run_in_progress"


def test_unknown_ticket_is_404(client):
    assert start(client, "tkt_nope").status_code == 404


def test_timeline_follows_the_run_and_is_redacted(client, work):
    tid = open_ticket(client, MESSAGE, "ord_1001")
    run_id = start(client, tid).json()["id"]
    work(full_script("refund_request", "ord_1001", "check_refund_eligibility", "propose_refund"))

    timeline = client.get(f"/staff/agent-runs/{run_id}/events", headers=as_staff()).json()
    kinds = [e["event_type"] for e in timeline]
    assert kinds[:2] == ["agent.queued", "job.claimed"]
    assert kinds[-1] == "agent.paused"  # waiting for a reviewer
    nodes = [e["redacted_payload"]["node"] for e in timeline if e["event_type"] == "node.completed"]
    assert nodes[:3] == ["load_ticket", "classify", "resolve_order"] and "record" in nodes
    assert "98450" not in json.dumps(timeline) and "headphones" not in json.dumps(timeline)


def test_timeline_needs_staff_and_a_real_run(client):
    tid = open_ticket(client, MESSAGE, "ord_1001")
    run_id = start(client, tid).json()["id"]
    assert client.get(f"/staff/agent-runs/{run_id}/events", headers=as_customer(ASHA)).status_code == 403
    assert client.get("/staff/agent-runs/run_nope/events", headers=as_staff()).status_code == 404


def test_finished_run_can_be_started_again(client, work):
    tid = open_ticket(client, "where is it?", "ord_1003")
    start(client, tid)
    work(full_script("order_status", "ord_1003", "get_shipment", "report_order_status"))
    assert start(client, tid).status_code == 202
