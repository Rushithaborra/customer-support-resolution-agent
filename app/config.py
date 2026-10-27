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


settings = Settings()
