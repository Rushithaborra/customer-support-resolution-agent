"""Retrieval queries built by CODE from the rules' own reason codes.

The model never writes the query that decides which policy is cited, so a
customer message can't steer citations. A reason code with no query here,
or a query that finds nothing above the cutoff, means "no applicable policy":
the ticket escalates instead of the agent inventing a rule."""

POLICY_QUERIES: dict[str, str] = {
    "eligible": "refund issued after the returned item is received and passes inspection",
    "no_return": "refund issued after the returned item is received and passes inspection",
    "return_not_received": "refund requested while the return is still in transit and has not arrived yet",
    "return_rejected": "returned item fails inspection and is not eligible for a refund",
    "inspection_pending": "returned item has arrived but inspection is still pending, usually within business days",
    "inspection_failed": "returned item fails inspection and is not eligible for a refund",
    "outside_return_window": "returned item reaches the warehouse after the return window days after delivery",
    "already_refunded": "order already refunded or refund in progress cannot be refunded again",
    "damage_report_in_window": "item arrived damaged reported within days of delivery reviewed by a specialist",
    "outside_damage_window": "damage reported after the reporting window days after delivery",
    "order_status": "order has not arrived delivery times and carrier tracking status",
}


def query_for(reason_code: str) -> str | None:
    return POLICY_QUERIES.get(reason_code)
