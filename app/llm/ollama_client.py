"""Ollama adapter: a free, local model (default qwen2.5:7b) behind the same LLM protocol.

The rest of the app speaks one conversation format (Anthropic-shaped turns and
tool_result blocks, see app/agent/loop.py). This module translates that to
Ollama's /api/chat format on the way out and back into LLMTurn on the way in,
so nothing outside app/llm/ knows which provider is running.
"""

import itertools
import json

import httpx

from app.config import Settings, settings as default_settings
from app.llm.types import LLMConfigError, LLMTurn, LLMUnavailable, ToolCall, Usage

_ids = itertools.count(1)


def _to_ollama_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    tool_names: dict[str, str] = {}  # tool_use id → tool name, for labelling results
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        blocks = [b if isinstance(b, dict) else b.model_dump() for b in content]
        if m["role"] == "assistant":
            calls = []
            for b in blocks:
                if b["type"] == "tool_use":
                    tool_names[b["id"]] = b["name"]
                    calls.append({"function": {"name": b["name"], "arguments": b["input"]}})
            msg = {"role": "assistant", "content": "".join(b.get("text", "") for b in blocks if b["type"] == "text")}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            for b in blocks:
                if b["type"] == "tool_result":
                    out.append({"role": "tool", "tool_name": tool_names.get(b["tool_use_id"], ""),
                                "content": b["content"]})
                elif b["type"] == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


def build_request(cfg: Settings, *, system: str, messages: list[dict], tools: list[dict] | None,
                  output_schema: dict | None) -> dict:
    """Pure: the exact JSON body sent to POST /api/chat."""
    body: dict = {
        "model": cfg.ollama_model,
        "messages": _to_ollama_messages(system, messages),
        "stream": False,
        # temperature 0 for repeatable runs; a larger context window than Ollama's default,
        # because system prompt + tool schemas + tool results don't fit in 2-4k tokens.
        "options": {"temperature": 0, "num_ctx": cfg.ollama_num_ctx, "num_predict": cfg.llm_max_tokens},
    }
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                           "parameters": t["input_schema"]}} for t in tools]
    if output_schema is not None:
        body["format"] = output_schema  # Ollama structured outputs: constrain to this JSON schema
    return body


def to_turn(data: dict) -> LLMTurn:
    msg = data.get("message") or {}
    text = msg.get("content") or ""
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):  # some models return arguments as a JSON string
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                pass  # left as-is: the tool's input model will reject it as invalid_input
        calls.append(ToolCall(c.get("id") or f"call_{next(_ids)}", fn.get("name", ""), args))
    raw = ([{"type": "text", "text": text}] if text else []) + \
          [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
    if calls:
        stop = "tool_use"
    elif data.get("done_reason") == "length":
        stop = "max_tokens"
    else:
        stop = "end_turn"
    usage = Usage(data.get("prompt_eval_count") or 0, data.get("eval_count") or 0)
    return LLMTurn(stop, text, calls, raw, data.get("model", ""), usage)


class OllamaLLM:
    def __init__(self, cfg: Settings = default_settings, client: httpx.Client | None = None):
        self._cfg = cfg
        self._http = client or httpx.Client(base_url=cfg.ollama_base_url,
                                            timeout=httpx.Timeout(cfg.ollama_timeout_s, connect=5.0))

    def complete(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                 output_schema: dict | None = None) -> LLMTurn:
        body = build_request(self._cfg, system=system, messages=messages, tools=tools, output_schema=output_schema)
        try:
            r = self._http.post("/api/chat", json=body)
        except httpx.ConnectError as e:
            raise LLMUnavailable("ollama is not running (start it with `ollama serve`)") from e
        except httpx.TransportError as e:
            raise LLMUnavailable(f"ollama: {type(e).__name__}") from e
        if r.status_code == 404:
            raise LLMConfigError(f"model not available: run `ollama pull {self._cfg.ollama_model}`")
        if r.status_code >= 500:
            raise LLMUnavailable(f"ollama: {r.status_code}")
        if r.status_code >= 400:
            raise LLMConfigError(f"ollama {r.status_code}: {r.text[:200]}")
        return to_turn(r.json())
