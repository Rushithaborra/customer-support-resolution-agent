"""Payment provider client.

Only the execution step (Part 11) may call create_refund, and only after a
stored approval. Nothing customer-facing or LLM-facing imports this module."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.integrations.http import ServiceClient, parse
from app.integrations.results import Ok, Result


class ProviderRefund(BaseModel):
    refund_id: str
    order_id: str
    amount_minor: int
    currency: str
    status: Literal["processing", "succeeded", "failed"]
    idempotency_key: str
    created_at: datetime


class PaymentClient:
    def __init__(self, http: ServiceClient):
        self._http = http

    def create_refund(self, *, order_id: str, amount_minor: int, idempotency_key: str,
                      currency: str = "INR") -> Result[ProviderRefund]:
        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
            raise TypeError("amount_minor must be an int in paise")
        result = self._http.call(
            "POST", "/payment/refunds", idempotency_key=idempotency_key,
            json={"order_id": order_id, "amount_minor": amount_minor, "currency": currency},
        )
        return parse(result, ProviderRefund, is_write=True)

    def get_refund(self, refund_id: str) -> Result[ProviderRefund]:
        return parse(self._http.call("GET", f"/payment/refunds/{refund_id}"), ProviderRefund, is_write=False)

    def find_refund_by_key(self, idempotency_key: str) -> Result[ProviderRefund | None]:
        """Reconciliation lookup: Ok(refund) if the provider applied it, Ok(None) if not."""
        result = self._http.call("GET", "/payment/refunds", params={"idempotency_key": idempotency_key})
        if not isinstance(result, Ok):
            return result
        matches = result.value.get("refunds", [])
        return Ok(ProviderRefund.model_validate(matches[0]) if matches else None)
