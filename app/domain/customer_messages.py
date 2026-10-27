"""What the customer is told. Built from stored, provider-confirmed state only,
so wording can never run ahead of reality ("completed" means the provider said so)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CustomerOutcome:
    state: str
    message: str


def format_inr(amount_minor: int) -> str:
    rupees, paise = divmod(amount_minor, 100)
    return f"₹{rupees:,}.{paise:02d}"


_REFUND_MESSAGES = {
    "reserved": "Your refund of {amt} has been approved and is being sent to our payment provider.",
    "submitted": "Your refund of {amt} has been submitted to our payment provider. "
                 "We'll confirm once they process it.",
    "processing": "Your refund of {amt} is being processed by our payment provider. It is not complete yet.",
    "succeeded": "Your refund of {amt} has been completed by our payment provider.",
    "failed": "We couldn't complete your refund of {amt}. Our team will follow up with you.",
    "pending_reconciliation": "Your refund of {amt} has been submitted. "
                              "We're confirming its status with our payment provider.",
}

_TICKET_MESSAGES = {
    "awaiting_approval": "Your refund request is awaiting approval.",
    "escalated": "Your request has been passed to a specialist, who will follow up with you.",
}


def customer_outcome(ticket_status: str, refund_status: str | None,
                     refund_amount_minor: int | None) -> CustomerOutcome | None:
    if refund_status is not None and refund_amount_minor is not None:
        msg = _REFUND_MESSAGES[refund_status].format(amt=format_inr(refund_amount_minor))
        return CustomerOutcome(refund_status, msg)
    if ticket_status in _TICKET_MESSAGES:
        return CustomerOutcome(ticket_status, _TICKET_MESSAGES[ticket_status])
    return None
