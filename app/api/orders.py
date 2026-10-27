from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_customer
from app.db.models import Customer
from app.db.session import get_db
from app.schemas import OrderOut
from app.services import orders

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=list[OrderOut])
def list_my_orders(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    return orders.list_orders(db, customer.id)


@router.get("/{order_id}", response_model=OrderOut)
def get_my_order(order_id: str, customer: Customer = Depends(get_current_customer),
                 db: Session = Depends(get_db)):
    return orders.get_owned_order(db, customer.id, order_id)
