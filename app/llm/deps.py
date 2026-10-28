from functools import lru_cache

from app.config import settings
from app.llm.types import LLM


def build_llm() -> LLM:
    """The configured backend (LLM_PROVIDER). Imports are lazy so neither SDK loads unless used."""
    if settings.llm_provider == "ollama":
        from app.llm.ollama_client import OllamaLLM
        return OllamaLLM()
    from app.llm.anthropic_client import AnthropicLLM
    return AnthropicLLM()


@lru_cache(maxsize=1)
def get_llm() -> LLM:
    """FastAPI dependency. Tests override it with a ScriptedLLM."""
    return build_llm()


def describe_llm() -> str:
    if settings.llm_provider == "ollama":
        return f"ollama:{settings.ollama_model}"
    return f"anthropic:{settings.llm_model}"
