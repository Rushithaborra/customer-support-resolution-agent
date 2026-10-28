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
    created_by: str | None


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


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    status: str
    attempts: int
    last_error: str | None


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ticket_id: str
    status: str
    route: str | None
    route_reason: str | None
    outcome: dict | None
    job: JobOut | None = None  # the latest background job working on this run


class RunEventOut(BaseModel):
    """One timeline entry. Payloads are already redacted (ids, node names, codes; no customer text)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor: str
    event_type: str
    redacted_payload: dict
    created_at: datetime


class RefundOperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    order_id: str
    proposal_id: str
    amount_minor: int
    idempotency_key: str
    provider_reference: str | None
    status: str
    reconcile_attempts: int = 0
    next_reconcile_at: datetime | None = None
    needs_review_reason: str | None = None  # set: the sweep stopped; a human must check with the provider


class DecisionOut(BaseModel):
    """The stored decision. For an agent proposal the paused workflow resumes in the
    background (resume_job_id); poll the run or the refund to see what it led to."""
    proposal: ProposalOut
    approval: ApprovalOut | None
    run_status: str | None
    refund: RefundOperationOut | None
    resume_job_id: str | None = None


class ExpiryOut(BaseModel):
    expired: list[str]
