"""Scripted-LLM building blocks shared by the agent tests."""

import json

from app.llm.fake import ScriptedLLM, text_turn, tool_turn
from tests.conftest import ASHA, as_customer


def triage(category: str, order_id: str | None, needs_human: bool = False):
    return text_turn(json.dumps({"category": category, "order_id": order_id, "customer_intent": "x",
                                 "needs_human": needs_human, "rationale": "r"}))


def summary(step: str, ref: str):
    return text_turn(json.dumps({"findings": [{"statement": "s", "evidence_ref": ref}],
                                 "recommended_next_step": step, "customer_reply_draft": "draft"}))


def draft(order_id: str, amount_minor: int = 249900, refs: list[str] | None = None):
    return text_turn(json.dumps({"order_id": order_id, "amount_minor": amount_minor, "currency": "INR",
                                 "evidence_refs": refs or [f"check_refund_eligibility:{order_id}"],
                                 "rationale": "Return received and inspection passed."}))


def full_script(category: str, order_id: str, tool: str, step: str) -> ScriptedLLM:
    # The trailing draft turn is only consumed when code routes to propose_refund.
    return ScriptedLLM([triage(category, order_id), tool_turn((tool, {"order_id": order_id})),
                        text_turn("notes"), summary(step, f"{tool}:{order_id}"),
                        draft(order_id, refs=[f"{tool}:{order_id}"])])


def open_ticket(client, message: str, order_id: str | None = None, who: str = ASHA) -> str:
    body = {"message": message} | ({"order_id": order_id} if order_id else {})
    r = client.post("/tickets", headers=as_customer(who), json=body)
    assert r.status_code == 201, r.json()
    return r.json()["id"]


def refund_script(draft_turn, *, tools=(("check_refund_eligibility", "ord_1001"), ("get_return_status", "ord_1001"))):
    return ScriptedLLM([
        triage("refund_request", "ord_1001"),
        tool_turn(*[(t, {"order_id": o}) for t, o in tools]),
        text_turn("Return received, inspection passed; code says eligible for 249900."),
        summary("propose_refund", "check_refund_eligibility:ord_1001"),
        draft_turn,
    ])
