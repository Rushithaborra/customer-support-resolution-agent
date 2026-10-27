"""Clients classify every failure, and never claim to know what they don't."""

import pytest

from app.integrations.helpdesk import HelpdeskClient
from app.integrations.http import ServiceClient
from app.integrations.payment import PaymentClient
from app.integrations.results import Ok, Rejected, Retryable, UnknownOutcome
from app.integrations.shipping import ShippingClient
from tests.conftest import CLIENT_TIMEOUT_S, _free_port, inject


@pytest.fixture
def payment(service_client):
    return PaymentClient(service_client)


def _create(payment, key="key-a", amount=249900):
    return payment.create_refund(order_id="ord_1001", amount_minor=amount, idempotency_key=key)


def test_refund_success(payment):
    r = _create(payment)
    assert isinstance(r, Ok)
    assert r.value.amount_minor == 249900
    assert r.value.status == "succeeded"
    assert not r.replayed


def test_timeout_after_apply_is_unknown_and_refund_exists(payment, mock_admin):
    inject(mock_admin, "timeout_after_apply", "payment.create_refund")
    assert isinstance(_create(payment, "key-t1"), UnknownOutcome)
    found = payment.find_refund_by_key("key-t1")
    assert isinstance(found, Ok) and found.value is not None  # it DID happen


def test_timeout_before_apply_is_unknown_and_refund_absent(payment, mock_admin):
    inject(mock_admin, "timeout_before_apply", "payment.create_refund")
    assert isinstance(_create(payment, "key-t2"), UnknownOutcome)
    found = payment.find_refund_by_key("key-t2")
    assert isinstance(found, Ok) and found.value is None  # it did NOT happen
    # Same error to the caller, opposite reality: only a lookup can tell them apart.


def test_retry_after_unknown_outcome_replays_not_duplicates(payment, mock_admin):
    inject(mock_admin, "timeout_after_apply", "payment.create_refund")
    assert isinstance(_create(payment, "key-t3"), UnknownOutcome)
    retry = _create(payment, "key-t3")
    assert isinstance(retry, Ok) and retry.replayed
    refunds = mock_admin.get("/payment/refunds", params={"idempotency_key": "key-t3"}).json()["refunds"]
    assert len(refunds) == 1


def test_503_is_retryable_and_nothing_applied(payment, mock_admin):
    inject(mock_admin, "error_503", "payment.create_refund")
    assert isinstance(_create(payment, "key-503"), Retryable)
    assert payment.find_refund_by_key("key-503").value is None


def test_500_on_write_is_unknown(payment, mock_admin):
    # A generic 5xx doesn't say whether the write was applied, so the client doesn't guess.
    inject(mock_admin, "error_500", "payment.create_refund")
    assert isinstance(_create(payment, "key-500"), UnknownOutcome)


def test_changed_payload_is_rejected(payment):
    _create(payment, "key-r")
    r = _create(payment, "key-r", amount=100)
    assert isinstance(r, Rejected)
    assert (r.status_code, r.code) == (422, "idempotency_key_reused")


def test_float_amount_never_leaves_the_client(payment):
    with pytest.raises(TypeError):
        payment.create_refund(order_id="ord_1001", amount_minor=2499.0, idempotency_key="key-f")


def test_processing_status_is_kept_distinct(payment, mock_admin):
    mock_admin.post("/_admin/payment/config", json={"new_refund_status": "processing"}).raise_for_status()
    created = _create(payment, "key-p")
    assert created.value.status == "processing"
    mock_admin.post(f"/_admin/payment/refunds/{created.value.refund_id}/settle",
                    json={"status": "succeeded"}).raise_for_status()
    assert payment.get_refund(created.value.refund_id).value.status == "succeeded"


def test_services_down_is_retryable():
    with ServiceClient(base_url=f"http://127.0.0.1:{_free_port()}",
                       connect_timeout_s=CLIENT_TIMEOUT_S, read_timeout_s=CLIENT_TIMEOUT_S) as c:
        assert isinstance(_create(PaymentClient(c), "key-down"), Retryable)


def test_wrong_api_key_is_rejected(mock_server_url):
    with ServiceClient(base_url=mock_server_url, api_key="wrong") as c:
        r = ShippingClient(c).get_shipment("ord_1003")
    assert isinstance(r, Rejected) and r.status_code == 401


def test_shipment_lookup(service_client):
    r = ShippingClient(service_client).get_shipment("ord_1003")
    assert isinstance(r, Ok)
    assert r.value.status == "in_transit" and r.value.eta is not None
    ret = ShippingClient(service_client).get_shipment("ord_1002").value.return_shipment
    assert ret.status == "in_transit"


def test_read_timeout_is_retryable(service_client, mock_admin):
    inject(mock_admin, "timeout_after_apply", "shipping.get_shipment")
    assert isinstance(ShippingClient(service_client).get_shipment("ord_1003"), Retryable)


def test_unknown_shipment_is_rejected(service_client):
    r = ShippingClient(service_client).get_shipment("ord_9999")
    assert isinstance(r, Rejected) and r.status_code == 404


def test_helpdesk_reply_retry_does_not_double_message(service_client, mock_admin):
    hd = HelpdeskClient(service_client)
    inject(mock_admin, "timeout_after_apply", "helpdesk.create_reply")
    first = hd.post_reply(ticket_external_id="hd_9", message="Refund approved.", idempotency_key="rep-1")
    assert isinstance(first, UnknownOutcome)
    retry = hd.post_reply(ticket_external_id="hd_9", message="Refund approved.", idempotency_key="rep-1")
    assert isinstance(retry, Ok) and retry.replayed
    assert len(hd.list_replies("hd_9").value) == 1
