"""Shared HTTP call + outcome classification for every external service.

The one rule that matters: for a *write*, anything other than a clear answer
from the provider is UnknownOutcome, because the provider may have applied it.
No retries happen here; the caller decides (Part 11 reconciles first)."""

from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.integrations.results import Ok, Rejected, Result, Retryable, UnknownOutcome

M = TypeVar("M", bound=BaseModel)

# The provider explicitly refused without doing anything: safe to retry even for writes.
_REFUSED_STATUSES = {429, 503}


class ServiceClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 connect_timeout_s: float | None = None, read_timeout_s: float | None = None):
        connect = connect_timeout_s if connect_timeout_s is not None else settings.http_connect_timeout_s
        read = read_timeout_s if read_timeout_s is not None else settings.http_read_timeout_s
        self._http = httpx.Client(
            base_url=base_url or settings.mock_base_url,
            headers={"Authorization": f"Bearer {api_key or settings.mock_api_key}"},
            timeout=httpx.Timeout(read, connect=connect),
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ServiceClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
             idempotency_key: str | None = None) -> Result[Any]:
        is_write = method.upper() != "GET"
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            r = self._http.request(method, path, json=json, params=params, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            # The request never left this process: nothing can have happened.
            return Retryable(f"not sent: {type(e).__name__}")
        except httpx.TransportError as e:
            # Sent, but no answer (read timeout, dropped connection).
            reason = f"no response: {type(e).__name__}"
            return UnknownOutcome(reason) if is_write else Retryable(reason)

        if r.is_success:
            return Ok(r.json(), replayed=r.headers.get("Idempotent-Replayed") == "true")
        if r.status_code in _REFUSED_STATUSES or r.status_code == 409:
            # 409 request_in_progress: the first attempt is still running under the same key.
            return Retryable(f"{r.status_code} {_error_code(r)}")
        if r.status_code >= 500:
            reason = f"{r.status_code} {_error_code(r)}"
            return UnknownOutcome(reason) if is_write else Retryable(reason)
        return Rejected(r.status_code, _error_code(r), _error_message(r))


def parse(result: Result[Any], model: type[M], *, is_write: bool, key: str | None = None) -> Result[M]:
    """Turn an Ok(dict) into Ok(model). A 2xx body we can't understand means we
    don't know what the provider did, which for a write is UnknownOutcome."""
    if not isinstance(result, Ok):
        return result
    try:
        data = result.value[key] if key else result.value
        return Ok(model.model_validate(data), replayed=result.replayed)
    except (KeyError, TypeError, ValidationError):
        reason = f"unparseable {model.__name__} response"
        return UnknownOutcome(reason) if is_write else Retryable(reason)


def _detail(r: httpx.Response) -> Any:
    try:
        return r.json().get("detail")
    except (ValueError, AttributeError):
        return None


def _error_code(r: httpx.Response) -> str:
    d = _detail(r)
    if isinstance(d, dict):
        return str(d.get("code", "error"))
    if isinstance(d, list):
        return "validation_error"
    return "error"


def _error_message(r: httpx.Response) -> str:
    d = _detail(r)
    if isinstance(d, dict):
        return str(d.get("message", ""))
    return str(d) if d is not None else r.text[:200]
