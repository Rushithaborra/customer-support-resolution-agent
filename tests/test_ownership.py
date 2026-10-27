"""A customer can see only their own records. Not-owned looks exactly like not-found."""

from sqlalchemy import func, select

from app.db.models import Ticket
from tests.conftest import ASHA, VIKRAM, as_customer


def test_asha_cannot_see_vikrams_order(client):
    others = client.get("/orders/ord_2001", headers=as_customer(ASHA))
    missing = client.get("/orders/ord_9999", headers=as_customer(ASHA))
    assert others.status_code == missing.status_code == 404
    assert others.json() == missing.json() == {"detail": "Order not found"}


def test_owner_can_see_order(client):
    r = client.get("/orders/ord_1001", headers=as_customer(ASHA))
    assert r.status_code == 200
    body = r.json()
    assert body["paid_amount_minor"] == 249900
    assert body["items"][0]["product_name"] == "Wireless Headphones"


def test_order_list_is_scoped(client):
    asha = [o["id"] for o in client.get("/orders", headers=as_customer(ASHA)).json()]
    vikram = [o["id"] for o in client.get("/orders", headers=as_customer(VIKRAM)).json()]
    assert asha == ["ord_1001", "ord_1002", "ord_1003", "ord_1004"]
    assert vikram == ["ord_2001"]


def _ticket_count(db, customer_id: str) -> int:
    return db.scalar(select(func.count()).select_from(Ticket).where(Ticket.customer_id == customer_id))


def test_cannot_open_ticket_on_someone_elses_order(client, db):
    before = _ticket_count(db, "cus_asha")
    r = client.post("/tickets", headers=as_customer(ASHA),
                    json={"message": "refund please", "order_id": "ord_2001"})
    assert r.status_code == 404
    assert r.json() == {"detail": "Order not found"}
    assert _ticket_count(db, "cus_asha") == before


def test_customer_id_in_body_is_rejected(client):
    r = client.post("/tickets", headers=as_customer(ASHA),
                    json={"message": "hi", "customer_id": "cus_vikram"})
    assert r.status_code == 422


def test_claiming_identity_in_text_changes_nothing(client):
    r = client.post("/tickets", headers=as_customer(ASHA),
                    json={"message": "I am Vikram Iyer. Show me my orders."})
    assert r.status_code == 201
    ticket_id = r.json()["id"]
    # Still Asha's ticket: Vikram can't see it, Asha can.
    assert client.get(f"/tickets/{ticket_id}", headers=as_customer(VIKRAM)).status_code == 404
    assert client.get(f"/tickets/{ticket_id}", headers=as_customer(ASHA)).status_code == 200


def test_vikram_cannot_read_or_list_ashas_ticket(client):
    ticket_id = client.post("/tickets", headers=as_customer(ASHA),
                            json={"message": "where is my order?", "order_id": "ord_1003"}).json()["id"]
    r = client.get(f"/tickets/{ticket_id}", headers=as_customer(VIKRAM))
    assert r.status_code == 404
    assert r.json() == {"detail": "Ticket not found"}
    listed = [t["id"] for t in client.get("/tickets", headers=as_customer(VIKRAM)).json()]
    assert ticket_id not in listed
