"""Eval cases: synthetic tickets with their own order fixtures and the outcome code should reach.

Each YAML file in evals/cases/ is one group. A case's `split` (dev | heldout) is fixed in
the file: tune against dev only, and report held-out separately.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models import Order, OrderItem, Return

CASES_DIR = Path(__file__).resolve().parent / "cases"
CUSTOMERS = {"asha": "cus_asha", "vikram": "cus_vikram"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReturnFx(_Strict):
    status: Literal["requested", "in_transit", "received", "rejected"]
    received_days_ago: int | None = None
    inspection: Literal["pending", "passed", "failed"] = "pending"


class ShipmentFx(_Strict):
    status: Literal["label_created", "in_transit", "out_for_delivery", "delivered", "lost"]
    eta_days: int | None = None
    delivered_days_ago: int | None = None


class OrderFx(_Strict):
    id: str = Field(pattern=r"^ord_e[0-9]+$")  # eval orders never collide with the seed
    customer: Literal["asha", "vikram"] = "asha"
    status: Literal["placed", "shipped", "delivered", "cancelled"]
    paid: int = Field(gt=0)
    product: str
    delivered_days_ago: int | None = None
    return_: ReturnFx | None = Field(default=None, alias="return")
    shipment: ShipmentFx | None = None


class Gold(_Strict):
    """The correct triage. The oracle model answers with this to check the case's labels."""
    category: Literal["order_status", "refund_request", "damaged_item", "unclassified"]
    order_id: str | None
    needs_human: bool = False


class Expect(_Strict):
    route: Literal["propose_refund", "explain_policy", "report_order_status", "escalate", "order_not_found"]
    reason: str | list[str]
    amount_minor: int | None = None

    @property
    def reasons(self) -> list[str]:
        return self.reason if isinstance(self.reason, list) else [self.reason]


class Case(_Strict):
    id: str
    group: str = ""  # from the file name
    split: Literal["dev", "heldout"]
    tags: list[str]
    customer: Literal["asha", "vikram"] = "asha"
    order: OrderFx | None = None
    orders: list[OrderFx] = []
    form_order: str | None
    message: str
    gold: Gold
    expect: Expect

    @model_validator(mode="after")
    def _consistent(self) -> "Case":
        if (self.expect.route == "propose_refund") != (self.expect.amount_minor is not None):
            raise ValueError("amount_minor is set exactly when a refund is expected")
        return self

    @property
    def fixtures(self) -> list[OrderFx]:
        return ([self.order] if self.order else []) + self.orders

    @property
    def customer_id(self) -> str:
        return CUSTOMERS[self.customer]


def load_cases(directory: Path = CASES_DIR) -> list[Case]:
    cases = []
    for path in sorted(directory.glob("*.yaml")):
        group = path.stem.split("_", 1)[1]  # 01_refund_eligible -> refund_eligible
        for raw in yaml.safe_load(path.read_text()):
            cases.append(Case.model_validate({**raw, "group": group}))
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return cases


# ---------------------------------------------------------------- fixtures → rows


def _ago(days: int | None, now: datetime) -> datetime | None:
    return None if days is None else now - timedelta(days=days)


def order_rows(fx: OrderFx, now: datetime) -> list:
    order = Order(id=fx.id, customer_id=CUSTOMERS[fx.customer], status=fx.status, currency="INR",
                  paid_amount_minor=fx.paid, delivered_at=_ago(fx.delivered_days_ago, now))
    rows = [order, OrderItem(id=f"itm_{fx.id[4:]}", order_id=fx.id, product_id=f"sku_{fx.id[4:]}",
                             product_name=fx.product, quantity=1, paid_amount_minor=fx.paid)]
    if fx.return_:
        r = fx.return_
        rows.append(Return(id=f"ret_{fx.id[4:]}", order_id=fx.id, status=r.status,
                           received_at=_ago(r.received_days_ago, now), inspection_status=r.inspection))
    return rows


def shipment_body(fx: OrderFx, now: datetime) -> dict | None:
    s = fx.shipment
    if s is None:
        return None
    return {"tracking_number": f"SS{fx.id[5:].upper()}", "status": s.status,
            "eta": None if s.eta_days is None else (now + timedelta(days=s.eta_days)).isoformat(),
            "delivered_at": None if s.delivered_days_ago is None else _ago(s.delivered_days_ago, now).isoformat()}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
