"""The Ollama adapter's translation and error mapping, without Ollama running."""

import json

import httpx
import pytest

from app.config import Settings
from app.llm.ollama_client import OllamaLLM, build_request, to_turn
from app.llm.types import LLMConfigError, LLMUnavailable

CFG = Settings(llm_provider="ollama", ollama_model="qwen2.5:7b", ollama_num_ctx=16384)
TOOL = {"name": "get_order", "description": "Get an order",
        "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}},
                         "required": ["order_id"], "additionalProperties": False}}

# The app's own conversation shape (what agent/loop.py builds), including a tool round trip.
CONVO = [
    {"role": "user", "content": "<customer_message>\nwhere is ord_1001?\n</customer_message>"},
    {"role": "assistant", "content": [{"type": "text", "text": "Checking."},
                                      {"type": "tool_use", "id": "call_7", "name": "get_order",
                                       "input": {"order_id": "ord_1001"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_7",
                                  "content": json.dumps({"ok": True, "data": {"status": "delivered"}})}]},
]


def test_request_translates_the_conversation():
    body = build_request(CFG, system="SYS", messages=CONVO, tools=[TOOL], output_schema=None)
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert body["messages"][2]["tool_calls"] == [{"function": {"name": "get_order", "arguments": {"order_id": "ord_1001"}}}]
    assert body["messages"][3]["tool_name"] == "get_order"  # result labelled with the tool it answers
    assert body["tools"][0]["function"]["parameters"] == TOOL["input_schema"]
    assert body["options"]["temperature"] == 0 and body["options"]["num_ctx"] == 16384
    assert body["stream"] is False and "format" not in body


def test_structured_request_uses_format_schema():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    body = build_request(CFG, system="S", messages=[{"role": "user", "content": "hi"}], tools=None,
                         output_schema=schema)
    assert body["format"] == schema and "tools" not in body


def test_tool_call_response_maps_to_turn():
    turn = to_turn({"model": "qwen2.5:7b", "done_reason": "stop", "prompt_eval_count": 900, "eval_count": 40,
                    "message": {"role": "assistant", "content": "",
                                "tool_calls": [{"function": {"name": "get_order",
                                                             "arguments": {"order_id": "ord_1001"}}}]}})
    assert turn.stop_reason == "tool_use"
    [call] = turn.tool_calls
    assert (call.name, call.input) == ("get_order", {"order_id": "ord_1001"}) and call.id.startswith("call_")
    assert turn.raw_content[-1]["type"] == "tool_use"  # appended back in the app's own shape
    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.model) == (900, 40, "qwen2.5:7b")


def test_string_arguments_are_parsed():
    turn = to_turn({"message": {"content": "", "tool_calls": [
        {"function": {"name": "get_order", "arguments": '{"order_id": "ord_1003"}'}}]}})
    assert turn.tool_calls[0].input == {"order_id": "ord_1003"}


def test_text_and_length_stops():
    assert to_turn({"message": {"content": "done"}, "done_reason": "stop"}).stop_reason == "end_turn"
    assert to_turn({"message": {"content": "cut"}, "done_reason": "length"}).stop_reason == "max_tokens"


def _llm(handler) -> OllamaLLM:
    return OllamaLLM(CFG, client=httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler)))


def test_round_trip_against_a_fake_server():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "qwen2.5:7b", "done_reason": "stop",
                                         "message": {"role": "assistant", "content": "Your order was delivered."}})
    turn = _llm(handler).complete(system="S", messages=CONVO, tools=[TOOL])
    assert turn.text == "Your order was delivered." and seen["body"]["model"] == "qwen2.5:7b"


@pytest.mark.parametrize("response, expected", [
    (httpx.Response(404, json={"error": "model not found"}), LLMConfigError),
    (httpx.Response(400, json={"error": "bad request"}), LLMConfigError),
    (httpx.Response(500, json={"error": "oom"}), LLMUnavailable),
])
def test_http_errors_map_to_our_two_kinds(response, expected):
    with pytest.raises(expected):
        _llm(lambda _req: response).complete(system="S", messages=[{"role": "user", "content": "hi"}])


def test_ollama_not_running_is_unavailable():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    with pytest.raises(LLMUnavailable, match="ollama serve"):
        _llm(refuse).complete(system="S", messages=[{"role": "user", "content": "hi"}])
