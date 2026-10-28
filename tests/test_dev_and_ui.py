"""Dev-only routes are gated, the UI is served, and the console's list endpoints are staff-only."""

import re
from pathlib import Path

import pytest

from app.config import settings
from app.integrations.payment import PaymentClient
from app.integrations.results import Retryable
from tests.agent_scripts import open_ticket
from tests.conftest import ASHA, as_customer, as_staff

WEB = Path(__file__).resolve().parent.parent / "web"


@pytest.fixture
def dev_mode(monkeypatch):
    monkeypatch.setattr(settings, "dev_mode", True)


@pytest.mark.parametrize("method, path", [("get", "/dev/info"), ("post", "/dev/token"), ("post", "/dev/faults"),
                                          ("post", "/dev/reset")])
def test_dev_routes_do_not_exist_without_dev_mode(client, method, path):
    assert getattr(client, method)(path).status_code == 404


def test_persona_token_is_a_normal_signed_token(client, dev_mode):
    t = client.post("/dev/token", json={"persona": "asha"}).json()
    assert t["role"] == "customer"
    me = client.get("/me", headers={"Authorization": f"Bearer {t['token']}"})
    assert me.json()["id"] == "cus_asha"
    ravi = client.post("/dev/token", json={"persona": "ravi"}).json()
    assert ravi["role"] == "reviewer"


def test_unknown_persona_is_rejected(client, dev_mode):
    assert client.post("/dev/token", json={"persona": "admin"}).status_code == 422


def test_dev_info_labels_the_model(client, dev_mode):
    info = client.get("/dev/info").json()
    assert info["model"] and {p["id"] for p in info["personas"]} == {"asha", "vikram", "priya", "ravi"}


def test_fault_is_forwarded_to_the_mock_provider(staff_client, dev_mode, service_client):
    assert staff_client.post("/dev/faults", json={"kind": "error_503"}).status_code == 200
    r = PaymentClient(service_client).create_refund(order_id="ord_1001", amount_minor=100, idempotency_key="k-dev")
    assert isinstance(r, Retryable)  # the next refund call hit the injected 503
    assert staff_client.post("/dev/faults", json={"kind": "drop_database"}).status_code == 422


def test_ui_pages_are_served(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/ui/"
    for page in ("/ui/", "/ui/customer.html", "/ui/console.html", "/ui/app.js", "/ui/style.css"):
        assert client.get(page).status_code == 200, page
    assert "Support Resolution Agent" in client.get("/ui/").text


def test_ui_never_writes_html_from_data():
    """Untrusted text (customer messages, tool output) must only ever reach the page as text."""
    for f in [*WEB.glob("*.js"), *WEB.glob("*.html")]:
        code = re.sub(r"//.*", "", f.read_text())
        assert not re.search(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write", code), f.name


def test_staff_ticket_queue(client):
    tid = open_ticket(client, "where is it?", "ord_1003")
    assert client.get("/staff/tickets", headers=as_customer(ASHA)).status_code == 403
    queue = client.get("/staff/tickets", headers=as_staff()).json()
    assert queue[0]["id"] == tid and "customer_id" in queue[0]
    assert all(t["status"] == "resolved" for t in client.get("/staff/tickets?status=resolved", headers=as_staff()).json())
    assert client.get("/staff/tickets?status=bogus", headers=as_staff()).status_code == 422


def test_runs_for_a_ticket(client):
    tid = open_ticket(client, "where is it?", "ord_1003")
    assert client.get(f"/staff/tickets/{tid}/agent-runs", headers=as_staff()).json() == []
    run_id = client.post(f"/staff/tickets/{tid}/agent-runs", headers=as_staff()).json()["id"]
    assert [r["id"] for r in client.get(f"/staff/tickets/{tid}/agent-runs", headers=as_staff()).json()] == [run_id]
    assert client.get("/staff/tickets/tkt_nope/agent-runs", headers=as_staff()).status_code == 404
