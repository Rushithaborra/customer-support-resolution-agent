"""Mock helpdesk: customer-visible replies on external tickets, posted idempotently
so a retried 'send reply' never messages the customer twice."""

import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from mocks.config import require_api_key
from mocks.faults import faults
from mocks.idempotency import IdempotencyStore, run_idempotent, run_read

router = APIRouter(prefix="/helpdesk", tags=["helpdesk"], dependencies=[Depends(require_api_key)])


class ReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)


_lock = threading.Lock()
_replies: dict[str, list[dict]] = {}
_keys = IdempotencyStore()


def reset() -> None:
    with _lock:
        _replies.clear()
    _keys.reset()


@router.post("/tickets/{external_id}/replies")
def create_reply(external_id: str, body: ReplyRequest, idempotency_key: str | None = Header(default=None)):
    fault = faults.take("helpdesk.create_reply")

    def apply() -> dict:
        reply = {
            "reply_id": f"hdr_{uuid.uuid4().hex[:12]}",
            "ticket_external_id": external_id,
            "message": body.message,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with _lock:
            _replies.setdefault(external_id, []).append(reply)
        return dict(reply)

    # The ticket id is part of the hashed request: same key on another ticket is a mismatch.
    payload = {"ticket_external_id": external_id, **body.model_dump()}
    return run_idempotent(_keys, idempotency_key, payload, fault, apply)


@router.get("/tickets/{external_id}/replies")
def list_replies(external_id: str):
    def read() -> dict:
        with _lock:
            return {"replies": [dict(r) for r in _replies.get(external_id, [])]}

    return run_read(faults.take("helpdesk.list_replies"), read)
