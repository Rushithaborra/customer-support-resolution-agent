"""Specialist mode: per-category investigators with code-enforced tool allow-lists."""

import pytest

from app.agent.tools import ToolContext, execute_tool, tool_specs
from app.integrations.shipping import ShippingClient
from app.llm.demo import DemoLLM
from app.llm.fake import ScriptedLLM, text_turn, tool_turn
from tests.agent_scripts import open_ticket, summary, triage
from tests.conftest import STAFF
from tests.test_demo_llm import SCENARIOS


def specialist_agent(db, service_client, checkpointer):
    from app.agent.runner import run_agent
    return lambda tid, llm: run_agent(db, llm, ShippingClient(service_client), tid, actor=f"staff:{STAFF}",
                                      checkpointer=checkpointer, mode="specialist")


@pytest.mark.parametrize("who, order_id, message, route", SCENARIOS)
def test_specialists_route_each_seed_scenario(client, db, service_client, checkpointer, who, order_id, message, route):
    run = specialist_agent(db, service_client, checkpointer)(open_ticket(client, message, order_id, who=who), DemoLLM())
    assert run.route == route
    assert run.outcome["agent_mode"] == "specialist"


def test_a_tool_outside_the_allow_list_is_refused_in_code(db, service_client):
    ctx = ToolContext(db=db, customer_id="cus_asha", shipping=ShippingClient(service_client),
                      allowed_tools=frozenset({"get_shipment"}))
    r = execute_tool(ctx, "check_refund_eligibility", {"order_id": "ord_1001"})
    assert r.code == "tool_not_allowed"
    assert [s["name"] for s in tool_specs(ctx.allowed_tools)] == ["get_shipment"]


def test_delivery_specialist_never_sees_refund_tools(client, db, service_client, checkpointer):
    """The model asks for a refund tool anyway (as an injected message might make it do): refused, and
    the request it got listed only the delivery specialist's tools."""
    llm = ScriptedLLM([triage("order_status", "ord_1003"),
                       tool_turn(("check_refund_eligibility", {"order_id": "ord_1003"})),
                       text_turn("could not check"), summary("report_order_status", "list_my_orders")])
    run = specialist_agent(db, service_client, checkpointer)(open_ticket(client, "where is it?", "ord_1003"), llm)
    offered = {t["name"] for t in llm.requests[1]["tools"]}
    assert offered == {"get_order", "get_shipment", "list_my_orders"}
    assert run.outcome["trace"][0] == {"tool": "check_refund_eligibility", "input": {"order_id": "ord_1003"},
                                       "ok": False, "code": "tool_not_allowed"}
    assert run.route == "report_order_status"  # code still routes from facts
    assert "specialist" in llm.requests[1]["system"]


def test_single_mode_is_the_default(client, agent):
    run = agent(open_ticket(client, "where is my speaker?", "ord_1003"), DemoLLM())
    assert run.outcome["agent_mode"] == "single"
