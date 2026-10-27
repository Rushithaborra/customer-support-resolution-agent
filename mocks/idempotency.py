"""Idempotency-key contract, modelled on how real payment APIs behave.

  new key                    -> apply, store (body hash, response), 201
  same key, same body        -> stored response, 200, Idempotent-Replayed: true
  same key, different body   -> 422 idempotency_key_reused, nothing applied
  same key, still running    -> 409 request_in_progress
  no key                     -> 400
"""

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from mocks.faults import Fault, delay_after_apply, delay_before_apply, fail_early


def body_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class _Entry:
    body_hash: str
    response: dict | None = None  # None while the first request is still running


class IdempotencyStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def begin(self, key: str, h: str) -> tuple[Literal["new", "replay", "mismatch", "in_progress"], _Entry]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = _Entry(body_hash=h)
                return "new", entry
            if entry.body_hash != h:
                return "mismatch", entry
            if entry.response is None:
                return "in_progress", entry
            return "replay", entry

    def complete(self, key: str, response: dict) -> None:
        with self._lock:
            self._entries[key].response = response

    def abandon(self, key: str) -> None:
        """Nothing was applied, so the key is free to be used again."""
        with self._lock:
            self._entries.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()


def run_idempotent(store: IdempotencyStore, key: str | None, payload: dict,
                   fault: Fault | None, apply: Callable[[], dict]) -> JSONResponse:
    if not key or len(key) > 64:
        raise HTTPException(400, detail={"code": "missing_idempotency_key",
                                         "message": "Idempotency-Key header (1-64 chars) is required"})
    fail_early(fault)  # a lost or failed request never reaches the key store

    outcome, entry = store.begin(key, body_hash(payload))
    if outcome == "replay":
        return JSONResponse(entry.response, status_code=200, headers={"Idempotent-Replayed": "true"})
    if outcome == "mismatch":
        raise HTTPException(422, detail={"code": "idempotency_key_reused",
                                         "message": "Key was already used with a different request body"})
    if outcome == "in_progress":
        raise HTTPException(409, detail={"code": "request_in_progress",
                                         "message": "A request with this key is still being processed"})
    try:
        delay_before_apply(fault)
        response = apply()
    except BaseException:
        store.abandon(key)
        raise
    store.complete(key, response)
    delay_after_apply(fault)
    return JSONResponse(response, status_code=201)


def run_read(fault: Fault | None, read: Callable[[], dict]) -> dict:
    fail_early(fault)
    delay_before_apply(fault)
    result = read()
    delay_after_apply(fault)
    return result
