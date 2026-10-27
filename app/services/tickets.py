"""Ticket creation and reads, scoped to the authenticated customer."""

from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ActionProposal, AuditEvent, Customer, RefundOperation, Ticket, WorkflowRun
from app.domain.customer_messages import customer_outcome
from app.services.errors import NotFound
from app.services.orders import get_owned_order

M = TypeVar("M", bound=BaseModel)


def latest_refund(db: Session, ticket_id: str) -> RefundOperation | None:
    return db.scalar(
        select(RefundOperation)
        .join(ActionProposal, RefundOperation.proposal_id == ActionProposal.id)
        .join(WorkflowRun, ActionProposal.run_id == WorkflowRun.id)
        .where(WorkflowRun.ticket_id == ticket_id)
        .order_by(RefundOperation.created_at.desc()).limit(1)
    )


def with_outcome(db: Session, ticket: Ticket, model: type[M]) -> M:
    """Serialize a ticket plus the customer-facing outcome, which is computed
    from stored refund state, never from free text."""
    refund = latest_refund(db, ticket.id)
    outcome = customer_outcome(ticket.status, refund.status if refund else None,
                               refund.amount_minor if refund else None)
    out = model.model_validate(ticket)
    out.outcome = None if outcome is None else {"state": outcome.state, "message": outcome.message}
    return out


def create_ticket(db: Session, customer: Customer, message: str, order_id: str | None) -> Ticket:
    """customer comes from the auth dependency, never from the request body.
    A referenced order must belong to that customer (else NotFound -> 404)."""
    if order_id is not None:
        get_owned_order(db, customer.id, order_id)

    ticket = Ticket(customer_id=customer.id, order_id=order_id, customer_message=message)
    db.add(ticket)
    db.flush()
    # Ids only: message text stays out of the audit log.
    db.add(AuditEvent(
        actor=f"customer:{customer.id}",
        event_type="ticket.created",
        redacted_payload={"ticket_id": ticket.id, "order_id": order_id},
    ))
    db.commit()  # ticket and audit row commit together or not at all
    return ticket


def list_tickets(db: Session, customer_id: str) -> Sequence[Ticket]:
    return db.scalars(
        select(Ticket).where(Ticket.customer_id == customer_id).order_by(Ticket.created_at.desc())
    ).all()


def get_owned_ticket(db: Session, customer_id: str, ticket_id: str) -> Ticket:
    ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id, Ticket.customer_id == customer_id))
    if ticket is None:
        raise NotFound("Ticket")
    return ticket
