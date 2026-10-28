from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or .env).
    Nothing secret is hard-coded in source."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://support:support@localhost:5432/support"
    store_currency: str = "INR"  # v1 scope: one store, one currency

    # Customer auth: HS256-signed JWTs. The default secret is for local dev only;
    # set JWT_SECRET in .env for anything else.
    jwt_secret: str = "dev-only-change-me-dev-only-change-me"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "support-dev-idp"
    jwt_audience: str = "support-api"
    jwt_ttl_minutes: int = 60

    # External services (mocked in v1: uv run uvicorn mocks.main:app --port 8001).
    mock_base_url: str = "http://127.0.0.1:8001"
    mock_api_key: str = "dev-mock-key"
    # Every external call has explicit timeouts; httpx's defaults are not a policy.
    http_connect_timeout_s: float = 2.0
    http_read_timeout_s: float = 5.0

    # Which model backend: "anthropic" (paid API), "ollama" (free, local) or "demo"
    # (deterministic keyword rules, not an AI: for offline, repeatable UI demos).
    llm_provider: Literal["anthropic", "ollama", "demo"] = "anthropic"
    demo_llm_delay_s: float = 0.8  # demo model: pause per call so the timeline fills in visibly

    # Local demo helpers (/dev/token persona login, /dev/faults). Never enable outside local dev.
    dev_mode: bool = False

    # Ollama (local). qwen2.5:7b handles tool calls and JSON output well for its size.
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_num_ctx: int = 16384
    ollama_timeout_s: float = 180.0  # local generation on a laptop is slow

    # LLM (Anthropic). The key is read here and passed to the SDK explicitly,
    # because pydantic-settings loads .env into Settings, not into os.environ.
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_tokens: int = 16000
    llm_timeout_s: float = 60.0
    # Server-side refusal fallback: a declined request is re-run on another model in the same call.
    llm_refusal_fallback: bool = True

    # Policy retrieval: chunks below this cosine similarity (bge-small-en-v1.5) are not
    # considered relevant. Calibrated from measured scores; see docs/parts/part-08-policy-rag.md.
    policy_min_similarity: float = 0.70
    # A persistent home for the embedding model (~130 MB). fastembed defaults to the OS temp
    # dir, which macOS clears, forcing a re-download.
    embedding_cache_dir: str = "~/.cache/fastembed"


settings = Settings()
