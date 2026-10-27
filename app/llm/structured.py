"""Structured output with repair-or-stop.

The API's schema mode guarantees valid JSON of the right *shape*. It can't
enforce everything (regex patterns, lengths and cross-field rules are moved
into descriptions), so every result is validated again by the Pydantic model.
Invalid output gets one repair attempt with the validation errors; if that
also fails, we stop and report. We never fill in a default or guess."""

from dataclasses import dataclass
from typing import Generic, TypeVar

from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

from app.llm.types import LLM, LLMUnavailable, Usage

T = TypeVar("T", bound=BaseModel)

REPAIR_PROMPT = (
    "Your previous JSON failed validation:\n{errors}\n"
    "Return corrected JSON only, matching the same schema."
)


@dataclass(frozen=True)
class Parsed(Generic[T]):
    value: T
    attempts: int
    model: str
    usage: Usage


@dataclass(frozen=True)
class StructuredFailure:
    reason: str  # invalid_output | refusal | truncated | llm_unavailable
    detail: str
    attempts: int
    usage: Usage


def strict_schema(model: type[BaseModel]) -> dict:
    return transform_schema(model)


def _summarize(e: ValidationError) -> str:
    # Locations and messages only. Input values are left out so we never echo untrusted text back.
    return "\n".join(f"- {'.'.join(map(str, err['loc'])) or '(root)'}: {err['msg']}" for err in e.errors()[:10])


def structured_call(llm: LLM, *, system: str, messages: list[dict], model: type[T],
                    max_repairs: int = 1) -> Parsed[T] | StructuredFailure:
    schema = strict_schema(model)
    convo = list(messages)
    usage = Usage()
    errors = ""
    for attempt in range(1, max_repairs + 2):
        try:
            turn = llm.complete(system=system, messages=convo, output_schema=schema)
        except LLMUnavailable as e:
            return StructuredFailure("llm_unavailable", str(e), attempt, usage)
        usage = usage + turn.usage
        if turn.stop_reason == "refusal":
            return StructuredFailure("refusal", "model declined", attempt, usage)
        if turn.stop_reason == "max_tokens":
            return StructuredFailure("truncated", "output hit max_tokens", attempt, usage)
        try:
            return Parsed(model.model_validate_json(turn.text), attempt, turn.model, usage)
        except ValidationError as e:
            errors = _summarize(e)
        convo = convo + [
            {"role": "assistant", "content": turn.raw_content},
            {"role": "user", "content": REPAIR_PROMPT.format(errors=errors)},
        ]
    return StructuredFailure("invalid_output", errors, max_repairs + 1, usage)
