"""Load the facts snapshot the eligibility rules run on."""

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.orm import Session

from app.db.models import REFUND_HOLDING_STATUSES, Order, RefundOperation, Return
from app.domain.eligibility import OrderFacts, ReturnFacts
from app.services.errors import NotFound


def held_refund_minor(db: Session, order_id: str) -> int:
    # Postgres SUM(bigint) returns NUMERIC (a Python Decimal). Cast back so money
    # stays an int all the way through.
    return db.scalar(
        select(cast(func.coalesce(func.sum(RefundOperation.amount_minor), 0), BigInteger))
        .where(RefundOperation.order_id == order_id,
               RefundOperation.status.in_(REFUND_HOLDING_STATUSES))
    )


def load_order_facts(db: Session, order_id: str, *, lock: bool = False) -> OrderFacts:
    """lock=True takes SELECT ... FOR UPDATE on the order row, serialising every
    reservation against the same order until the caller commits."""
    q = select(Order).where(Order.id == order_id)
    if lock:
        # populate_existing: refresh from the locked row even if the session already
        # holds a (possibly stale) copy of this order.
        q = q.with_for_update().execution_options(populate_existing=True)
    order = db.scalar(q)
    if order is None:
        raise NotFound("Order")

    ret = db.scalar(
        select(Return).where(Return.order_id == order_id)
        .order_by(Return.created_at.desc(), Return.id.desc()).limit(1)
    )
    return OrderFacts(
        order_id=order.id,
        status=order.status,
        currency=order.currency,
        paid_amount_minor=order.paid_amount_minor,
        delivered_at=order.delivered_at,
        latest_return=ReturnFacts(ret.id, ret.status, ret.inspection_status, ret.received_at) if ret else None,
        held_minor=held_refund_minor(db, order_id),
    )
