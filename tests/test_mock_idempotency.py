"""The provider-side idempotency contract (Part 3 done-when)."""

from concurrent.futures import ThreadPoolExecutor

from tests.conftest import inject

REFUND = {"order_id": "ord_1001", "amount_minor": 249900, "currency": "INR"}


def _refund(admin, key: str | None, body: dict = REFUND):
    headers = {"Idempotency-Key": key} if key else {}
    return admin.post("/payment/refunds", json=body, headers=headers)


def _refunds_for(admin, key: str) -> list[dict]:
    return admin.get("/payment/refunds", params={"idempotency_key": key}).json()["refunds"]


def test_same_key_same_body_returns_same_operation(mock_admin):
    first = _refund(mock_admin, "rfd-key-1")
    second = _refund(mock_admin, "rfd-key-1")
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    assert len(_refunds_for(mock_admin, "rfd-key-1")) == 1


def test_same_key_changed_body_is_rejected(mock_admin):
    original = _refund(mock_admin, "rfd-key-2").json()
    r = _refund(mock_admin, "rfd-key-2", {**REFUND, "amount_minor": 999900})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "idempotency_key_reused"
    stored = _refunds_for(mock_admin, "rfd-key-2")
    assert stored == [original]  # nothing new applied, original unchanged


def test_missing_key_is_rejected(mock_admin):
    r = _refund(mock_admin, None)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "missing_idempotency_key"


def test_float_amount_is_rejected(mock_admin):
    assert _refund(mock_admin, "rfd-key-f", {**REFUND, "amount_minor": 2499.0}).status_code == 422


def test_concurrent_identical_requests_store_one_refund(mock_admin):
    # Slow the first request so the second arrives while it is still in progress.
    inject(mock_admin, "slow", "payment.create_refund", delay_s=0.5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(f.result().status_code for f in
                       [pool.submit(_refund, mock_admin, "rfd-key-c") for _ in range(2)])
    assert codes == [201, 409]
    assert len(_refunds_for(mock_admin, "rfd-key-c")) == 1


def test_failed_request_frees_the_key(mock_admin):
    inject(mock_admin, "error_503", "payment.create_refund")
    assert _refund(mock_admin, "rfd-key-3").status_code == 503
    assert _refunds_for(mock_admin, "rfd-key-3") == []
    assert _refund(mock_admin, "rfd-key-3").status_code == 201


def test_helpdesk_reply_is_idempotent_per_ticket(mock_admin):
    url = "/helpdesk/tickets/hd_1/replies"
    a = mock_admin.post(url, json={"message": "Your refund is awaiting approval."},
                        headers={"Idempotency-Key": "reply-1"})
    b = mock_admin.post(url, json={"message": "Your refund is awaiting approval."},
                        headers={"Idempotency-Key": "reply-1"})
    assert (a.status_code, b.status_code) == (201, 200)
    assert len(mock_admin.get(url).json()["replies"]) == 1
    # Same key on a different ticket is a different request, so it's rejected.
    other = mock_admin.post("/helpdesk/tickets/hd_2/replies", json={"message": "Your refund is awaiting approval."},
                            headers={"Idempotency-Key": "reply-1"})
    assert other.status_code == 422


def test_mocks_require_api_key(mock_server_url):
    import httpx
    r = httpx.get(f"{mock_server_url}/shipping/shipments/ord_1003")
    assert r.status_code == 401
