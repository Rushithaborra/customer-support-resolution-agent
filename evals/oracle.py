"""The oracle: a model that always triages correctly (the case's gold answer) and investigates
with the right tools. Run through the real graph, it shows what CODE does with a perfect model,
so a case whose oracle run misses its `expect` has a wrong label, not a weak model."""

import json

from app.llm.demo import MODEL, DemoLLM, _TOOL_PLAN
from app.llm.fake import text_turn, tool_turn
from app.llm.types import LLMTurn
from evals.cases import Case


class OracleLLM(DemoLLM):
    def __init__(self, case: Case):
        super().__init__(delay_s=0)
        self.case = case

    def _triage(self, content: str) -> LLMTurn:
        g = self.case.gold
        return text_turn(json.dumps({"category": g.category, "order_id": g.order_id, "customer_intent": "gold",
                                     "needs_human": g.needs_human, "rationale": "oracle"}), model=MODEL)

    def _investigate(self, content: str, messages: list[dict]) -> LLMTurn:
        if len(messages) == 1:
            order = content.split("order under investigation: ", 1)[1].split()[0]
            plan = _TOOL_PLAN.get(self.case.gold.category, ("get_order",))
            return tool_turn(*[(tool, {"order_id": order}) for tool in plan], model=MODEL)
        return super()._investigate(content, messages)
