"""The agent-run endpoints: staff only, and the result can be read back."""

from tests.conftest import ASHA, as_customer, as_staff
from tests.agent_scripts import full_script, open_ticket


def test_customer_cannot_run_the_agent(client):
    tid = open_ticket(client, "refund?", "ord_1001")
    assert client.post(f"/staff/tickets/{tid}/agent-runs", headers=as_customer(ASHA)).status_code == 403


def test_staff_runs_agent_and_reads_result(staff_client, work):
    tid = open_ticket(staff_client, "I returned my headphones, where's my refund?", "ord_1001")
    r = staff_client.post(f"/staff/tickets/{tid}/agent-runs", headers=as_staff())
    assert r.status_code == 202  # recorded; the worker does the work
    queued = r.json()
    assert r.headers["Location"] == f"/staff/agent-runs/{queued['id']}"
    assert (queued["status"], queued["route"], queued["job"]["status"]) == ("queued", None, "queued")

    work(full_script("refund_request", "ord_1001", "check_refund_eligibility", "propose_refund"))
    run = staff_client.get(f"/staff/agent-runs/{queued['id']}", headers=as_staff()).json()
    assert (run["route"], run["route_reason"], run["status"]) == ("propose_refund", "eligible", "paused")
    assert (run["job"]["status"], run["job"]["attempts"]) == ("succeeded", 1)
    assert run["outcome"]["proposal_id"].startswith("prp_")
    assert run["outcome"]["trace"][0]["tool"] == "check_refund_eligibility"


def test_unknown_run_is_404(client):
    assert client.get("/staff/agent-runs/run_nope", headers=as_staff()).status_code == 404
