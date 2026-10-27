from fastapi import Header, HTTPException
from pydantic_settings import BaseSettings, SettingsConfigDict


class MockSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mock_api_key: str = "dev-mock-key"


mock_settings = MockSettings()


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    """Server-to-server auth, like a provider's secret API key."""
    if authorization != f"Bearer {mock_settings.mock_api_key}":
        raise HTTPException(status_code=401, detail={"code": "invalid_api_key", "message": "Invalid API key"})
