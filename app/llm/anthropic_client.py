"""Anthropic adapter: the only module that imports the SDK."""

import anthropic

from app.config import Settings, settings as default_settings
from app.llm.types import LLMConfigError, LLMTurn, LLMUnavailable, ToolCall, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def build_request(cfg: Settings, *, system: str, messages: list[dict], tools: list[dict] | None,
                  output_schema: dict | None) -> dict:
    """Pure: the exact kwargs sent to messages.create. Unit-tested without the network."""
    request: dict = {
        "model": cfg.llm_model,
        "max_tokens": cfg.llm_max_tokens,
        "system": system,
        "messages": messages,
        "output_config": {"effort": cfg.llm_effort},
    }
    if tools:
        # strict: the API guarantees tool inputs match the schema. tool_choice stays
        # "auto": forcing a specific tool is rejected on current models.
        request["tools"] = [{**t, "strict": True} for t in tools]
        request["tool_choice"] = {"type": "auto"}
    if output_schema is not None:
        request["output_config"]["format"] = {"type": "json_schema", "schema": output_schema}
    if cfg.llm_refusal_fallback:
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"
    return request


def to_turn(response) -> LLMTurn:
    text = "".join(b.text for b in response.content if b.type == "text")
    calls = [ToolCall(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"]
    usage = Usage(response.usage.input_tokens or 0, response.usage.output_tokens or 0)
    return LLMTurn(response.stop_reason or "", text, calls, list(response.content), response.model, usage)


class AnthropicLLM:
    def __init__(self, cfg: Settings = default_settings, client: anthropic.Anthropic | None = None):
        self._cfg = cfg
        # api_key=None lets the SDK resolve credentials itself (env var or `ant auth login` profile).
        self._client = client or anthropic.Anthropic(
            api_key=cfg.anthropic_api_key or None, timeout=cfg.llm_timeout_s, max_retries=2)

    def complete(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                 output_schema: dict | None = None) -> LLMTurn:
        request = build_request(self._cfg, system=system, messages=messages, tools=tools,
                                output_schema=output_schema)
        try:
            response = self._client.beta.messages.create(**request)
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            raise LLMUnavailable(f"network: {type(e).__name__}") from e
        except (anthropic.RateLimitError, anthropic.OverloadedError, anthropic.ServiceUnavailableError,
                anthropic.InternalServerError) as e:
            raise LLMUnavailable(f"provider: {e.status_code}") from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.BadRequestError,
                anthropic.NotFoundError, anthropic.UnprocessableEntityError) as e:
            raise LLMConfigError(f"{e.status_code}: {e.message}") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise LLMUnavailable(f"provider: {e.status_code}") from e
            raise LLMConfigError(f"{e.status_code}: {e.message}") from e
        return to_turn(response)
