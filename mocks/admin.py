"""Test/demo controls. Not part of any real provider's API."""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from mocks import data, helpdesk, payment
from mocks.config import require_api_key
from mocks.faults import Fault, FaultKind, faults

router = APIRouter(prefix="/_admin", tags=["admin"], dependencies=[Depends(require_api_key)])


class FaultRequest(BaseModel):
    kind: FaultKind
    operation: str | None = None
    delay_s: float = Field(default=10.0, ge=0, le=60)
    count: int = Field(default=1, ge=1, le=100)


class PaymentConfig(BaseModel):
    new_refund_status: payment.RefundStatus


class SettleRequest(BaseModel):
    status: Literal["succeeded", "failed"]


@router.post("/faults", status_code=204)
def add_fault(body: FaultRequest):
    faults.add(Fault(kind=body.kind, delay_s=body.delay_s, operation=body.operation, remaining=body.count))


@router.post("/reset", status_code=204)
def reset():
    faults.reset()
    payment.reset()
    helpdesk.reset()
    data.reset_extra_shipments()


class ShipmentIn(BaseModel):
    carrier: str = "SwiftShip"
    tracking_number: str
    status: Literal["label_created", "in_transit", "out_for_delivery", "delivered", "lost"]
    eta: str | None = None
    delivered_at: str | None = None
    return_shipment: dict | None = None


@router.post("/shipping/shipments/{order_id}", status_code=204)
def register_shipment(order_id: str, body: ShipmentIn):
    """Evals create their own synthetic orders; this gives the carrier tracking for them."""
    data.SHIPMENTS[order_id] = body.model_dump()


@router.post("/payment/config", status_code=204)
def configure_payment(body: PaymentConfig):
    payment.set_new_refund_status(body.new_refund_status)


@router.post("/payment/refunds/{refund_id}/settle")
def settle_refund(refund_id: str, body: SettleRequest):
    return payment.settle(refund_id, body.status)
