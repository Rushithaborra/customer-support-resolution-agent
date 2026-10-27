"""The tool-use loop: model calls read tools, code runs them, repeat until done.
It always returns a LoopResult with an explicit stop reason; the caller decides
what a non-normal stop means (Part 6 routes those to a human)."""

import json
from dataclasses import dataclass, field

from app.agent.prompts import wrap_customer_message
from app.agent.tools import ToolContext, ToolSuccess, execute_tool, tool_specs
from app.llm.types import LLM, LLMUnavailable, Usage


@dataclass(frozen=True)
class TraceEntry:
    tool: str
    input: object
    ok: bool
    code: str | None


@dataclass
class LoopResult:
    stop: str  # end_turn | step_limit | refusal | max_tokens | llm_unavailable | <other stop_reason>
    text: str
    trace: list[TraceEntry] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def _tool_result_block(tool_use_id: str, result) -> dict:
    if isinstance(result, ToolSuccess):
        return {"type": "tool_result", "tool_use_id": tool_use_id,
                "content": json.dumps({"ok": True, "data": result.data})}
    return {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
            "content": json.dumps({"ok": False, "error": {"code": result.code, "message": result.message,
                                                          "retryable": result.retryable}})}


def run_tool_loop(llm: LLM, ctx: ToolContext, *, system: str, user_message: str,
                  max_steps: int = 8, preface: str | None = None) -> LoopResult:
    """preface: trusted, system-verified context (e.g. which order code resolved),
    kept outside the untrusted customer-message block."""
    content = wrap_customer_message(user_message)
    if preface:
        content = f"{preface}\n\n{content}"
    messages: list[dict] = [{"role": "user", "content": content}]
    specs = tool_specs()
    out = LoopResult(stop="step_limit", text="")

    for _ in range(max_steps):
        try:
            turn = llm.complete(system=system, messages=messages, tools=specs)
        except LLMUnavailable:
            out.stop = "llm_unavailable"
            return out
        out.models.append(turn.model)
        out.usage = out.usage + turn.usage

        if turn.stop_reason != "tool_use" or not turn.tool_calls:
            out.stop, out.text = turn.stop_reason, turn.text
            return out

        # Full assistant content goes back unchanged; all results return in ONE user message.
        messages.append({"role": "assistant", "content": turn.raw_content})
        results = []
        for call in turn.tool_calls:
            result = execute_tool(ctx, call.name, call.input)
            out.trace.append(TraceEntry(call.name, call.input, isinstance(result, ToolSuccess),
                                        getattr(result, "code", None)))
            results.append(_tool_result_block(call.id, result))
        messages.append({"role": "user", "content": results})

    return out  # step_limit
