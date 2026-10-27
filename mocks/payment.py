"""Mock payment provider: refunds with idempotency keys and reconciliation lookup."""

import threading
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from mocks.config import require_api_key
from mocks.faults import faults
from mocks.idempotency import IdempotencyStore, run_idempotent, run_read

router = APIRouter(prefix="/payment", tags=["payment"], dependencies=[Depends(require_api_key)])

RefundStatus = Literal["processing", "succeeded", "failed"]


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1, max_length=32)
    amount_minor: StrictInt = Field(gt=0)  # StrictInt: 2499.0 is rejected, not coerced
    currency: Literal["INR"]


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refunds: dict[str, dict] = {}
        self.new_refund_status: RefundStatus = "succeeded"


_state = _State()
_keys = IdempotencyStore()


def reset() -> None:
    global _state
    _state = _State()
    _keys.reset()


def set_new_refund_status(status: RefundStatus) -> None:
    _state.new_refund_status = status


def settle(refund_id: str, status: Literal["succeeded", "failed"]) -> dict:
    """Move a 'processing' refund to its final state, as a provider does later."""
    with _state.lock:
        refund = _state.refunds.get(refund_id)
        if refund is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "Refund not found"})
        if refund["status"] != "processing":
            raise HTTPException(409, detail={"code": "already_final", "message": "Refund is already final"})
        refund["status"] = status
        return dict(refund)


@router.post("/refunds")
def create_refund(body: RefundRequest, idempotency_key: str | None = Header(default=None)):
    fault = faults.take("payment.create_refund")

    def apply() -> dict:
        refund = {
            "refund_id": f"pref_{uuid.uuid4().hex[:12]}",
            **body.model_dump(),
            "status": _state.new_refund_status,
            "idempotency_key": idempotency_key,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with _state.lock:
            _state.refunds[refund["refund_id"]] = refund
        return dict(refund)

    return run_idempotent(_keys, idempotency_key, body.model_dump(), fault, apply)


@router.get("/refunds/{refund_id}")
def get_refund(refund_id: str):
    def read() -> dict:
        with _state.lock:
            refund = _state.refunds.get(refund_id)
        if refund is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "Refund not found"})
        return dict(refund)

    return run_read(faults.take("payment.get_refund"), read)


@router.get("/refunds")
def find_refunds(idempotency_key: str):
    """Reconciliation: 'did a refund with this key happen?' Asked after a timeout,
    before deciding whether to retry."""
    def read() -> dict:
        with _state.lock:
            matches = [dict(r) for r in _state.refunds.values() if r["idempotency_key"] == idempotency_key]
        return {"refunds": matches}

    return run_read(faults.take("payment.find_refunds"), read)
