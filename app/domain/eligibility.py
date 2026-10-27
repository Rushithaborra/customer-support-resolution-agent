"""Eligibility rules as pure functions over a facts snapshot.

The rules run in a fixed order and the first failing rule decides, so every
"no" has exactly one reason code the reviewer and the customer can see."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.policy import DAMAGE_REPORT_WINDOW_DAYS, RETURN_WINDOW_DAYS


@dataclass(frozen=True)
class ReturnFacts:
    return_id: str
    status: str             # requested | in_transit | received | rejected
    inspection_status: str  # pending | passed | failed
    received_at: datetime | None


@dataclass(frozen=True)
class OrderFacts:
    order_id: str
    status: str             # placed | shipped | delivered | cancelled
    currency: str
    paid_amount_minor: int
    delivered_at: datetime | None
    latest_return: ReturnFacts | None
    held_minor: int         # sum of refund operations that still hold balance

    @property
    def refundable_minor(self) -> int:
        return max(self.paid_amount_minor - self.held_minor, 0)


@dataclass(frozen=True)
class RefundDecision:
    eligible: bool
    reason_code: str
    refundable_minor: int


@dataclass(frozen=True)
class DamageDecision:
    recommendation: str  # escalate | not_eligible
    reason_code: str


def evaluate_refund(f: OrderFacts) -> RefundDecision:
    def no(reason: str) -> RefundDecision:
        return RefundDecision(False, reason, 0)

    if f.status != "delivered" or f.delivered_at is None:
        return no("not_delivered")
    r = f.latest_return
    if r is None:
        return no("no_return")
    if r.status == "rejected":
        return no("return_rejected")
    if r.status != "received" or r.received_at is None:
        return no("return_not_received")
    if r.inspection_status == "pending":
        return no("inspection_pending")
    if r.inspection_status == "failed":
        return no("inspection_failed")
    if r.received_at - f.delivered_at > timedelta(days=RETURN_WINDOW_DAYS):
        return no("outside_return_window")
    if f.refundable_minor <= 0:
        return no("already_refunded")
    return RefundDecision(True, "eligible", f.refundable_minor)


def assess_damage(f: OrderFacts, now: datetime) -> DamageDecision:
    """Damage claims always need a human to look at evidence in v1; code only
    decides whether the claim is inside the reporting window."""
    if f.status != "delivered" or f.delivered_at is None:
        return DamageDecision("not_eligible", "not_delivered")
    if now - f.delivered_at > timedelta(days=DAMAGE_REPORT_WINDOW_DAYS):
        return DamageDecision("not_eligible", "outside_damage_window")
    return DamageDecision("escalate", "damage_report_in_window")
