"""Shipping carrier client. Callers must check order ownership first:
the carrier will answer about any order id it is asked about."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.integrations.http import ServiceClient, parse
from app.integrations.results import Result

ShipmentStatus = Literal["label_created", "in_transit", "out_for_delivery", "delivered", "exception"]


class ReturnShipment(BaseModel):
    tracking_number: str
    status: ShipmentStatus
    delivered_at: datetime | None


class Shipment(BaseModel):
    order_id: str
    carrier: str
    tracking_number: str
    status: ShipmentStatus
    eta: datetime | None
    delivered_at: datetime | None
    return_shipment: ReturnShipment | None


class ShippingClient:
    def __init__(self, http: ServiceClient):
        self._http = http

    def get_shipment(self, order_id: str) -> Result[Shipment]:
        return parse(self._http.call("GET", f"/shipping/shipments/{order_id}"), Shipment, is_write=False)
