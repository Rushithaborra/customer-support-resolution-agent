"""The Anthropic adapter's request shape, response mapping and error mapping, without the network."""

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from app.config import Settings
from app.llm.anthropic_client import FALLBACK_BETA, AnthropicLLM, build_request, to_turn
from app.llm.types import LLMConfigError, LLMUnavailable

CFG = Settings(llm_model="claude-opus-5-5", llm_effort="medium", llm_max_tokens=16000)
TOOL = {"name": "get_order", "description": "d",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False, "required": []}}


def test_tool_request_shape():
    req = build_request(CFG, system="s", messages=[{"role": "user", "content": "hi"}], tools=[TOOL],
                        output_schema=None)
    assert req["model"] == "claude-opus-5-5" and req["max_tokens"] == 16000
    assert req["tools"][0]["strict"] is True
    assert req["tool_choice"] == {"type": "auto"}  # forced choice is rejected on current models
    assert req["output_config"] == {"effort": "medium"}
    assert (req["betas"], req["fallbacks"]) == ([FALLBACK_BETA], "default")
    assert not {"temperature", "top_p", "top_k", "thinking"} & req.keys()


def test_structured_request_shape():
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    req = build_request(CFG, system="s", messages=[], tools=None, output_schema=schema)
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": schema}
    assert "tools" not in req and "tool_choice" not in req


def test_fallback_can_be_turned_off():
    req = build_request(Settings(llm_refusal_fallback=False), system="s", messages=[], tools=None,
                        output_schema=None)
    assert "fallbacks" not in req and "betas" not in req


def _message(content, stop="tool_use", model="claude-opus-5-5") -> BetaMessage:
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 120, "output_tokens": 30}})


def test_response_maps_to_turn():
    msg = _message([{"type": "text", "text": "Checking."},
                    {"type": "tool_use", "id": "toolu_1", "name": "get_order", "input": {"order_id": "ord_1001"}}])
    turn = to_turn(msg)
    assert turn.stop_reason == "tool_use" and turn.text == "Checking."
    assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [("toolu_1", "get_order", {"order_id": "ord_1001"})]
    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.model) == (120, 30, "claude-opus-5-5")
    assert turn.raw_content == list(msg.content)  # appended back unchanged


class _Raising:
    def __init__(self, exc):
        self.beta = self
        self.messages = self
        self._exc = exc

    def create(self, **_):
        raise self._exc


_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status(cls, code):
    return cls("err", response=httpx2.Response(code, request=_REQ), body=None)


@pytest.mark.parametrize("exc, expected", [
    (anthropic.APITimeoutError(request=_REQ), LLMUnavailable),
    (anthropic.APIConnectionError(request=_REQ), LLMUnavailable),
    (_status(anthropic.RateLimitError, 429), LLMUnavailable),
    (_status(anthropic.InternalServerError, 500), LLMUnavailable),
    (_status(anthropic.OverloadedError, 529), LLMUnavailable),
    (_status(anthropic.AuthenticationError, 401), LLMConfigError),
    (_status(anthropic.BadRequestError, 400), LLMConfigError),
    (_status(anthropic.NotFoundError, 404), LLMConfigError),
])
def test_sdk_errors_map_to_our_two_kinds(exc, expected):
    llm = AnthropicLLM(CFG, client=_Raising(exc))
    with pytest.raises(expected):
        llm.complete(system="s", messages=[{"role": "user", "content": "hi"}])
