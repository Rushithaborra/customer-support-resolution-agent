"""A deterministic, rule-based stand-in for the LLM (LLM_PROVIDER=demo).

For running the UI demos offline and repeatably. It is not an AI: keyword rules
classify, a fixed tool plan investigates, and the summary and draft are built
from the tool results it was given. It gets no special powers. It talks to the
same tools through the same loop, and code still decides the route, eligibility
and the amount, so a demo shows exactly what the guarantees do with any model.
"""

import json
import re
import time

from app.llm.fake import text_turn, tool_turn
from app.llm.types import LLMTurn

MODEL = "demo-rules"

_ORDER = re.compile(r"\bord_[a-z0-9]+\b")
_FORM_ORDER = re.compile(r"(?:selected in the ticket form|order under investigation): (ord_[a-z0-9]+)")
_OK_CALL = re.compile(r"^- ([a-z_]+)\(\{'order_id': '(ord_[a-z0-9]+)'\}\): ok$", re.M)

# First match wins: damage beats refund ("broken, refund me" is a damage claim).
_CATEGORIES = [
    ("damaged_item", ("crack", "broken", "damaged", "defect", "shattered", "dead on arrival")),
    ("refund_request", ("refund", "returned", "return", "sent back", "send back", "money back", "my money")),
    ("order_status", ("where is", "where's", "arrive", "track", "deliver", "shipping", "status")),
]
_TOOL_PLAN = {
    "refund_request": ("check_refund_eligibility", "get_return_status"),
    "order_status": ("get_shipment",),
    "damaged_item": ("get_order", "get_return_status"),
}


def _customer_text(content: str) -> str:
    m = re.search(r"<customer_message>\n(.*)\n</customer_message>", content, re.S)
    return m.group(1) if m else content


def _categorize(text: str) -> str:
    low = text.lower()
    for category, words in _CATEGORIES:
        if any(w in low for w in words):
            return category
    return "unclassified"


def _first_user_text(messages: list[dict]) -> str:
    first = messages[0]["content"]
    return first if isinstance(first, str) else ""


class DemoLLM:
    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s  # so a demo's timeline visibly fills in, step by step

    def complete(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                 output_schema: dict | None = None) -> LLMTurn:
        if self.delay_s:
            time.sleep(self.delay_s)
        props = (output_schema or {}).get("properties", {})
        content = _first_user_text(messages)
        if "category" in props:
            return self._triage(content)
        if "findings" in props:
            return self._summary(content)
        if "evidence_refs" in props:
            return self._draft(content)
        if tools:
            return self._investigate(content, messages)
        return text_turn("No action.", model=MODEL)

    def _triage(self, content: str) -> LLMTurn:
        text = _customer_text(content)
        written, form = _ORDER.search(text), _FORM_ORDER.search(content)
        order_id = written.group(0) if written else (form.group(1) if form else None)
        category = _categorize(text)
        needs_human = category == "unclassified" or order_id is None
        return text_turn(json.dumps({
            "category": category, "order_id": order_id, "customer_intent": text.strip()[:200] or "unclear",
            "needs_human": needs_human, "rationale": f"keyword rules matched {category}"}), model=MODEL)

    def _investigate(self, content: str, messages: list[dict]) -> LLMTurn:
        order = _FORM_ORDER.search(content)
        if len(messages) == 1 and order:
            plan = _TOOL_PLAN.get(_categorize(_customer_text(content)), ("get_order",))
            return tool_turn(*[(tool, {"order_id": order.group(1)}) for tool in plan], model=MODEL)
        results = [json.loads(b["content"]) for m in messages if isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        return text_turn("Tool results: " + json.dumps(results, sort_keys=True), model=MODEL)

    def _summary(self, content: str) -> LLMTurn:
        ok_calls = _OK_CALL.findall(content)
        findings = [{"statement": f"{tool} returned a result for {order}", "evidence_ref": f"{tool}:{order}"}
                    for tool, order in ok_calls] or [{"statement": "No tool returned data", "evidence_ref":
                                                      "list_my_orders"}]
        category = _categorize(_customer_text(content))
        if category == "refund_request":
            step = "propose_refund" if '"eligible": true' in content else "explain_policy"
        else:
            step = {"order_status": "report_order_status", "damaged_item": "escalate"}.get(category, "escalate")
        return text_turn(json.dumps({"findings": findings[:10], "recommended_next_step": step,
                                     "customer_reply_draft": "Thanks for your patience. Here is what we found."}),
                         model=MODEL)

    def _draft(self, content: str) -> LLMTurn:
        order = re.search(r"Draft a refund proposal for order (ord_[a-z0-9]+)", content).group(1)
        amount = re.search(r'"refundable_minor": (\d+)', content)
        refs = [f"{tool}:{o}" for tool, o in _OK_CALL.findall(content)] or [f"check_refund_eligibility:{order}"]
        return text_turn(json.dumps({"order_id": order, "amount_minor": int(amount.group(1)) if amount else 0,
                                     "currency": "INR", "evidence_refs": refs[:10],
                                     "rationale": "Code says the order is eligible; return received and inspected."}),
                         model=MODEL)
