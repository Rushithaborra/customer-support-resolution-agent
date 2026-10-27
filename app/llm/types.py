"""Provider-neutral shapes the rest of the app depends on. Only app/llm/anthropic_client.py
knows about the SDK; everything else (and every test) uses these."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: Any  # untrusted: validated by the tool's input model before use


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


@dataclass(frozen=True)
class LLMTurn:
    stop_reason: str             # end_turn | tool_use | max_tokens | refusal | ...
    text: str
    tool_calls: list[ToolCall]
    raw_content: list            # appended back verbatim on the next request
    model: str                   # the model that actually answered (may be a fallback)
    usage: Usage = field(default_factory=Usage)


class LLMUnavailable(Exception):
    """Transient: rate limit, overload, 5xx, timeout, network. The caller may retry later or escalate."""


class LLMConfigError(Exception):
    """Our fault: bad key, bad request, unknown model. Retrying won't help; fix the config."""


class LLM(Protocol):
    def complete(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                 output_schema: dict | None = None) -> LLMTurn: ...
