"""API request/response models. Money stays integer minor units (paise)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str
    product_name: str
    quantity: int
    paid_amount_minor: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    currency: str
    paid_amount_minor: int
    delivered_at: datetime | None
    items: list[OrderItemOut]


class TicketCreate(BaseModel):
    # extra="forbid": a body that tries to set customer_id (or anything else) is a 422,
    # not silently ignored. Identity only ever comes from the token.
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    order_id: str | None = Field(default=None, max_length=32)


class OutcomeOut(BaseModel):
    """Customer-facing status, derived from stored confirmed state only."""
    state: str
    message: str


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    order_id: str | None
    category: str
    status: str
    customer_message: str
    created_at: datetime
    outcome: OutcomeOut | None = None


# ---------------------------------------------------------------- staff


class StaffTicketOut(TicketOut):
    customer_id: str
    summary: str | None


class ClassifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal["order_status", "refund_request", "damaged_item", "unclassified"]


class ProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    action_type: str
    payload: dict
    payload_hash: str
    status: str


class ApproveIn(BaseModel):
    # The hash of the payload the reviewer actually looked at.
    model_config = ConfigDict(extra="forbid")

    payload_hash: str = Field(min_length=64, max_length=64)


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    proposal_id: str
    approved_payload_hash: str
    reviewer_id: str | None
    decision: str
    expires_at: datetime


class CloseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["resolved", "escalated"]
    summary: str = Field(min_length=1, max_length=2000)


class RefundOperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    order_id: str
    proposal_id: str
    amount_minor: int
    idempotency_key: str
    provider_reference: str | None
    status: str
