"""Every tool returns a structured success or failure; identity and ownership are code."""

import pytest
from sqlalchemy import select

from app.agent.tools import TOOLS, ToolContext, ToolFailure, ToolSuccess, execute_tool, tool_specs
from app.db.models import AuditEvent
from app.integrations.shipping import ShippingClient
from tests.conftest import inject


@pytest.fixture
def ctx(db, service_client):
    return ToolContext(db=db, customer_id="cus_asha", shipping=ShippingClient(service_client))


def test_catalog_is_read_only_and_fixed():
    assert set(TOOLS) == {"list_my_orders", "get_order", "get_return_status", "get_refund_status",
                          "get_shipment", "check_refund_eligibility"}
    for spec in tool_specs():
        schema = spec["input_schema"]
        assert schema["additionalProperties"] is False
        assert "required" in schema
        assert not any("customer" in p for p in schema.get("properties", {})), spec["name"]


def test_list_my_orders_is_scoped(ctx):
    r = execute_tool(ctx, "list_my_orders", {})
    assert isinstance(r, ToolSuccess)
    assert [o["order_id"] for o in r.data["orders"]] == ["ord_1001", "ord_1002", "ord_1003", "ord_1004"]


def test_get_order(ctx):
    r = execute_tool(ctx, "get_order", {"order_id": "ord_1001"})
    assert isinstance(r, ToolSuccess)
    assert r.data["paid_amount_minor"] == 249900
    assert r.data["items"] == [{"product_name": "Wireless Headphones", "quantity": 1}]


def test_someone_elses_order_looks_missing(ctx):
    others = execute_tool(ctx, "get_order", {"order_id": "ord_2001"})
    missing = execute_tool(ctx, "get_order", {"order_id": "ord_9999"})
    assert others == missing == ToolFailure("not_found", "Order not found")


@pytest.mark.parametrize("tool", ["get_return_status", "get_refund_status", "get_shipment",
                                  "check_refund_eligibility"])
def test_every_order_tool_checks_ownership(ctx, tool):
    assert execute_tool(ctx, tool, {"order_id": "ord_2001"}) == ToolFailure("not_found", "Order not found")


def test_return_status(ctx):
    r = execute_tool(ctx, "get_return_status", {"order_id": "ord_1002"})
    assert r.data["return"]["status"] == "in_transit"
    assert execute_tool(ctx, "get_return_status", {"order_id": "ord_1003"}).data["return"] is None


def test_eligibility_comes_from_code(ctx):
    ok = execute_tool(ctx, "check_refund_eligibility", {"order_id": "ord_1001"}).data
    assert (ok["eligible"], ok["refundable_minor"], ok["decided_by"]) == (True, 249900, "code")
    no = execute_tool(ctx, "check_refund_eligibility", {"order_id": "ord_1002"}).data
    assert (no["eligible"], no["reason_code"]) == (False, "return_not_received")


def test_refund_status_empty(ctx):
    assert execute_tool(ctx, "get_refund_status", {"order_id": "ord_1001"}).data["refunds"] == []


def test_shipment(ctx):
    r = execute_tool(ctx, "get_shipment", {"order_id": "ord_1003"})
    assert isinstance(r, ToolSuccess) and r.data["status"] == "in_transit"


def test_shipping_down_is_retryable_failure(ctx, mock_admin):
    inject(mock_admin, "error_503", "shipping.get_shipment")
    r = execute_tool(ctx, "get_shipment", {"order_id": "ord_1003"})
    assert isinstance(r, ToolFailure) and (r.code, r.retryable) == ("unavailable", True)


@pytest.mark.parametrize("raw", [
    {"order_id": "ord_1001", "customer_id": "cus_vikram"},  # can't smuggle identity in
    {"order_id": "ORD-1001"},                                # pattern enforced by Pydantic
    {"order_id": 1001},
    {},
    "ord_1001",
])
def test_invalid_input_is_a_failure_not_an_exception(ctx, raw):
    r = execute_tool(ctx, "get_order", raw)
    assert isinstance(r, ToolFailure) and r.code == "invalid_input"
    assert "cus_vikram" not in r.message  # input values are never echoed back


def test_unknown_tool(ctx):
    assert execute_tool(ctx, "issue_refund", {"order_id": "ord_1001"}).code == "unknown_tool"


def test_unexpected_error_is_contained(ctx, monkeypatch):
    def boom(*_):
        raise RuntimeError("db exploded")
    monkeypatch.setattr("app.agent.tools.list_orders", boom)
    r = execute_tool(ctx, "list_my_orders", {})
    assert r == ToolFailure("internal_error", "Tool failed")


def test_tool_calls_are_audited(ctx, db):
    execute_tool(ctx, "get_order", {"order_id": "ord_2001"})
    ev = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "tool.called")).all()[-1]
    assert ev.actor == "agent"
    assert ev.redacted_payload == {"tool": "get_order", "ok": False, "code": "not_found", "order_id": "ord_2001"}
