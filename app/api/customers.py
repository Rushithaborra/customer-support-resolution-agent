from fastapi import APIRouter, Depends

from app.auth import get_current_customer
from app.db.models import Customer
from app.schemas import CustomerOut

router = APIRouter(tags=["customers"])


@router.get("/me", response_model=CustomerOut)
def me(customer: Customer = Depends(get_current_customer)):
    return customer
