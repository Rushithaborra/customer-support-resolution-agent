from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth import get_current_customer
from app.db.models import Customer
from app.db.session import get_db
from app.schemas import TicketCreate, TicketOut
from app.services import tickets

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create(body: TicketCreate, customer: Customer = Depends(get_current_customer),
           db: Session = Depends(get_db)):
    return tickets.create_ticket(db, customer, body.message, body.order_id)


@router.get("", response_model=list[TicketOut])
def list_mine(customer: Customer = Depends(get_current_customer), db: Session = Depends(get_db)):
    return tickets.list_tickets(db, customer.id)


@router.get("/{ticket_id}", response_model=TicketOut)
def get_mine(ticket_id: str, customer: Customer = Depends(get_current_customer),
             db: Session = Depends(get_db)):
    return tickets.get_owned_ticket(db, customer.id, ticket_id)
