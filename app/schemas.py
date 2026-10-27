"""API request/response models. Money stays integer minor units (paise)."""

from datetime import datetime

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


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    order_id: str | None
    category: str
    status: str
    customer_message: str
    created_at: datetime
