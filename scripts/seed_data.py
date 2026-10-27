"""Seed synthetic data. Every order exists to exercise one scenario.

Fixed IDs (not random) so tests, evals and demos can refer to them.
Run:  uv run python -m scripts.seed_data
Safe to re-run: it wipes business tables first.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db.models import Customer, Order, OrderItem, Return
from app.db.session import SessionLocal

NOW = datetime.now(timezone.utc)
DAYS = lambda n: NOW - timedelta(days=n)  # noqa: E731

CUSTOMERS = [
    Customer(id="cus_asha", external_auth_subject="auth|asha", name="Asha Rao", email="asha@example.com"),
    Customer(id="cus_vikram", external_auth_subject="auth|vikram", name="Vikram Iyer", email="vikram@example.com"),
]

# (order, items, return_or_None, scenario description)
SCENARIOS = [
    (
        Order(id="ord_1001", customer_id="cus_asha", status="delivered", currency="INR",
              paid_amount_minor=249900, delivered_at=DAYS(12)),
        [OrderItem(id="itm_1001a", order_id="ord_1001", product_id="sku_hp200",
                   product_name="Wireless Headphones", quantity=1, paid_amount_minor=249900)],
        Return(id="ret_1001", order_id="ord_1001", status="received",
               received_at=DAYS(3), inspection_status="passed"),
        "ELIGIBLE: return received, inspection passed -> refund after approval",
    ),
    (
        Order(id="ord_1002", customer_id="cus_asha", status="delivered", currency="INR",
              paid_amount_minor=89900, delivered_at=DAYS(6)),
        [OrderItem(id="itm_1002a", order_id="ord_1002", product_id="sku_kb10",
                   product_name="Mechanical Keyboard", quantity=1, paid_amount_minor=89900)],
        Return(id="ret_1002", order_id="ord_1002", status="in_transit",
               received_at=None, inspection_status="pending"),
        "BLOCKED: return not yet received -> explain, no refund",
    ),
    (
        Order(id="ord_1003", customer_id="cus_asha", status="shipped", currency="INR",
              paid_amount_minor=159900, delivered_at=None),
        [OrderItem(id="itm_1003a", order_id="ord_1003", product_id="sku_sp50",
                   product_name="Bluetooth Speaker", quantity=1, paid_amount_minor=159900)],
        None,
        "ORDER STATUS: in transit -> report shipment status",
    ),
    (
        Order(id="ord_1004", customer_id="cus_asha", status="delivered", currency="INR",
              paid_amount_minor=349900, delivered_at=DAYS(2)),
        [OrderItem(id="itm_1004a", order_id="ord_1004", product_id="sku_mn24",
                   product_name="24-inch Monitor", quantity=1, paid_amount_minor=349900)],
        None,
        "DAMAGED ITEM: delivered recently -> collect photos/details, resolve or escalate",
    ),
    (
        Order(id="ord_2001", customer_id="cus_vikram", status="delivered", currency="INR",
              paid_amount_minor=129900, delivered_at=DAYS(40)),
        [OrderItem(id="itm_2001a", order_id="ord_2001", product_id="sku_ms30",
                   product_name="Gaming Mouse", quantity=1, paid_amount_minor=129900)],
        Return(id="ret_2001", order_id="ord_2001", status="received",
               received_at=DAYS(5), inspection_status="failed"),
        "BLOCKED: inspection failed -> not eligible, escalate if customer disputes. "
        "Also used for AUTHZ tests: Asha must NOT be able to see this order.",
    ),
]


def main() -> None:
    with SessionLocal() as db, db.begin():
        # Child tables first (TRUNCATE ... CASCADE handles FK order for us).
        db.execute(text(
            "TRUNCATE audit_events, refund_operations, approvals, action_proposals, "
            "workflow_runs, tickets, returns, order_items, orders, customers CASCADE"
        ))
        db.add_all(CUSTOMERS)
        db.flush()  # customers must exist before orders reference them
        for order, items, ret, _ in SCENARIOS:
            db.add(order)
            db.flush()
            db.add_all(items)
            if ret:
                db.add(ret)

    print("Seeded:")
    for order, _, _, desc in SCENARIOS:
        print(f"  {order.id} ({order.customer_id}): {desc}")


if __name__ == "__main__":
    main()
