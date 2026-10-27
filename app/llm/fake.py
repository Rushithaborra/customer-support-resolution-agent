"""A scripted LLM for tests: replays prepared turns and records every request,
so tests are free, deterministic, and can assert exactly what was sent."""

import copy
import itertools

from app.llm.types import LLMTurn, ToolCall, Usage

_ids = itertools.count(1)


def text_turn(text: str, stop_reason: str = "end_turn", model: str = "scripted") -> LLMTurn:
    return LLMTurn(stop_reason, text, [], [{"type": "text", "text": text}], model, Usage(10, 5))


def tool_turn(*calls: tuple[str, object], model: str = "scripted") -> LLMTurn:
    tool_calls = [ToolCall(f"toolu_{next(_ids)}", name, inp) for name, inp in calls]
    raw = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in tool_calls]
    return LLMTurn("tool_use", "", tool_calls, raw, model, Usage(10, 5))


class ScriptedLLM:
    def __init__(self, turns: list[LLMTurn | Exception]):
        self._turns = list(turns)
        self.requests: list[dict] = []

    def complete(self, **request) -> LLMTurn:
        self.requests.append(copy.deepcopy(request))
        if not self._turns:
            raise AssertionError("ScriptedLLM: script exhausted (the code under test made an extra call)")
        nxt = self._turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
