"""Order reads. Every query is scoped to the owning customer in SQL,
so there is no code path that loads an order and checks ownership afterwards."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Order
from app.services.errors import NotFound


def get_owned_order(db: Session, customer_id: str, order_id: str) -> Order:
    order = db.scalar(
        select(Order)
        .where(Order.id == order_id, Order.customer_id == customer_id)
        .options(selectinload(Order.items))
    )
    if order is None:
        raise NotFound("Order")
    return order


def list_orders(db: Session, customer_id: str) -> Sequence[Order]:
    return db.scalars(
        select(Order)
        .where(Order.customer_id == customer_id)
        .options(selectinload(Order.items))
        .order_by(Order.id)
    ).all()
