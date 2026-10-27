"""Mock shipping carrier: read-only tracking.

No customer ownership here: a carrier trusts its API-key holder. Our app
checks ownership before it ever asks the carrier about an order."""

from fastapi import APIRouter, Depends, HTTPException

from mocks.config import require_api_key
from mocks.data import SHIPMENTS
from mocks.faults import faults
from mocks.idempotency import run_read

router = APIRouter(prefix="/shipping", tags=["shipping"], dependencies=[Depends(require_api_key)])


@router.get("/shipments/{order_id}")
def get_shipment(order_id: str):
    def read() -> dict:
        shipment = SHIPMENTS.get(order_id)
        if shipment is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "Shipment not found"})
        return {"order_id": order_id, **shipment}

    return run_read(faults.take("shipping.get_shipment"), read)
