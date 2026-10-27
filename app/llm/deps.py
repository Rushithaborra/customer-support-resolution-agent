from functools import lru_cache

from app.llm.anthropic_client import AnthropicLLM
from app.llm.types import LLM


@lru_cache(maxsize=1)
def get_llm() -> LLM:
    """FastAPI dependency. Tests override it with a ScriptedLLM."""
    return AnthropicLLM()
