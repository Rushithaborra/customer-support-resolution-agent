"""Staff routes need a staff token; customer tokens can't reach them, and vice versa."""

from tests.conftest import ASHA, as_customer, as_staff


def _ticket(client) -> str:
    return client.post("/tickets", headers=as_customer(ASHA), json={"message": "hi"}).json()["id"]


def test_no_token_is_401(client):
    assert client.get(f"/staff/tickets/{_ticket(client)}").status_code == 401


def test_customer_token_is_403(client):
    r = client.get(f"/staff/tickets/{_ticket(client)}", headers=as_customer(ASHA))
    assert r.status_code == 403
    assert r.json() == {"detail": "Staff access required"}


def test_customer_cannot_approve_or_execute(client):
    for path in ["/staff/proposals/prp_x/approve", "/staff/proposals/prp_x/execute",
                 "/staff/refunds/rfd_x/refresh"]:
        assert client.post(path, headers=as_customer(ASHA), json={"payload_hash": "x" * 64}).status_code == 403


def test_staff_token_works(client):
    r = client.get(f"/staff/tickets/{_ticket(client)}", headers=as_staff())
    assert r.status_code == 200
    assert r.json()["customer_id"] == "cus_asha"


def test_staff_token_is_not_a_customer(client):
    assert client.get("/me", headers=as_staff()).status_code == 401
