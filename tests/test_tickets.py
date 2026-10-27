"""Ticket lifecycle for Part 2: create, list, get, with an audit record."""

from sqlalchemy import select

from app.db.models import AuditEvent, Ticket
from tests.conftest import ASHA, as_customer


def test_create_list_get(client, db):
    r = client.post("/tickets", headers=as_customer(ASHA),
                    json={"message": "I returned my headphones but no refund yet", "order_id": "ord_1001"})
    assert r.status_code == 201
    created = r.json()
    assert created["status"] == "open"
    assert created["category"] == "unclassified"  # classification arrives in Part 6
    assert created["order_id"] == "ord_1001"

    stored = db.get(Ticket, created["id"])
    assert stored.customer_id == "cus_asha"

    listed = client.get("/tickets", headers=as_customer(ASHA)).json()
    assert created["id"] in [t["id"] for t in listed]

    fetched = client.get(f"/tickets/{created['id']}", headers=as_customer(ASHA))
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_ticket_without_order(client):
    r = client.post("/tickets", headers=as_customer(ASHA), json={"message": "general question"})
    assert r.status_code == 201
    assert r.json()["order_id"] is None


def test_creation_is_audited_without_message_text(client, db):
    ticket_id = client.post("/tickets", headers=as_customer(ASHA),
                            json={"message": "secret-ish detail", "order_id": "ord_1001"}).json()["id"]
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "ticket.created",
                                 AuditEvent.redacted_payload["ticket_id"].astext == ticket_id)
    ).all()
    assert len(events) == 1
    assert events[0].actor == "customer:cus_asha"
    assert events[0].redacted_payload == {"ticket_id": ticket_id, "order_id": "ord_1001"}


def test_message_validation(client):
    assert client.post("/tickets", headers=as_customer(ASHA), json={"message": ""}).status_code == 422
    assert client.post("/tickets", headers=as_customer(ASHA), json={"message": "x" * 4001}).status_code == 422
