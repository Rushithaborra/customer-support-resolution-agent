"""Staff and reviewer routes. Operating a ticket needs a staff (or reviewer) token;
deciding a proposal needs a reviewer token, and never the proposal's creator."""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agent.checkpoint import get_checkpointer
from app.agent.resume import resume_for_proposal, resume_superseded
from app.auth import Staff, get_current_reviewer, get_current_staff
from app.db.session import get_db
from app.integrations.deps import get_payment_client, get_shipping_client
from app.integrations.payment import PaymentClient
from app.integrations.shipping import ShippingClient
from app.policy.embedder import Embedder, get_embedder
from app.schemas import (ApproveIn, ClassifyIn, CloseIn, DecisionOut, ExpiryOut, ProposalOut,
                         RefundOperationOut, StaffTicketOut)
from app.services import approvals, refunds, resolution
from app.services.tickets import with_outcome

router = APIRouter(prefix="/staff", tags=["staff"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/tickets/{ticket_id}", response_model=StaffTicketOut)
def get_ticket(ticket_id: str, _: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    return with_outcome(db, resolution.get_ticket(db, ticket_id), StaffTicketOut)


@router.patch("/tickets/{ticket_id}", response_model=StaffTicketOut)
def classify(ticket_id: str, body: ClassifyIn, staff: Staff = Depends(get_current_staff),
             db: Session = Depends(get_db)):
    return with_outcome(db, resolution.classify(db, staff, ticket_id, body.category), StaffTicketOut)


@router.get("/tickets/{ticket_id}/assessment")
def assess(ticket_id: str, _: Staff = Depends(get_current_staff), db: Session = Depends(get_db),
           shipping: ShippingClient = Depends(get_shipping_client)):
    return resolution.assess(db, ticket_id, shipping, _now())


@router.post("/tickets/{ticket_id}/proposals", response_model=ProposalOut, status_code=201)
def propose(ticket_id: str, staff: Staff = Depends(get_current_staff), db: Session = Depends(get_db),
            embedder: Embedder = Depends(get_embedder), checkpointer=Depends(get_checkpointer),
            payment: PaymentClient = Depends(get_payment_client)):
    proposal = resolution.propose_refund(db, staff, ticket_id, embedder)
    # Any agent proposal this one superseded: let its paused workflow finish as "invalidated".
    resume_superseded(db, ticket_id, checkpointer=checkpointer, payment=payment)
    return proposal


@router.post("/tickets/{ticket_id}/close", response_model=StaffTicketOut)
def close(ticket_id: str, body: CloseIn, staff: Staff = Depends(get_current_staff),
          db: Session = Depends(get_db)):
    ticket = resolution.close_ticket(db, staff, ticket_id, body.outcome, body.summary)
    return with_outcome(db, ticket, StaffTicketOut)


@router.get("/proposals", response_model=list[ProposalOut])
def list_proposals(status: Literal["pending_approval", "approved", "rejected", "expired", "superseded",
                                   "executed"] | None = None,
                   _: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    """Reviewer queue: ?status=pending_approval shows what's waiting for a human."""
    return resolution.list_proposals(db, status)


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
def get_proposal(proposal_id: str, _: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    return resolution.get_proposal(db, proposal_id)


def _decision(db: Session, proposal_id: str, approval=None, run=None) -> dict:
    return {"proposal": resolution.get_proposal(db, proposal_id),
            "approval": approval,
            "run_status": run.status if run else None,
            "refund": refunds.operation_for(db, proposal_id)}


@router.post("/proposals/{proposal_id}/approve", response_model=DecisionOut)
def approve(proposal_id: str, body: ApproveIn, reviewer: Staff = Depends(get_current_reviewer),
            db: Session = Depends(get_db), checkpointer=Depends(get_checkpointer),
            payment: PaymentClient = Depends(get_payment_client)):
    """Store the approval, then resume the paused agent workflow (if any), which executes the refund."""
    approval = approvals.approve(db, reviewer, proposal_id, body.payload_hash, _now())
    run = resume_for_proposal(db, proposal_id, "approved", checkpointer=checkpointer, payment=payment)
    return _decision(db, proposal_id, approval, run)


@router.post("/proposals/{proposal_id}/reject", response_model=DecisionOut)
def reject(proposal_id: str, reviewer: Staff = Depends(get_current_reviewer), db: Session = Depends(get_db),
           checkpointer=Depends(get_checkpointer), payment: PaymentClient = Depends(get_payment_client)):
    approvals.reject(db, reviewer, proposal_id, _now())
    run = resume_for_proposal(db, proposal_id, "rejected", checkpointer=checkpointer, payment=payment)
    return _decision(db, proposal_id, None, run)


@router.post("/maintenance/expire-proposals", response_model=ExpiryOut)
def expire_proposals(_: Staff = Depends(get_current_staff), db: Session = Depends(get_db),
                     checkpointer=Depends(get_checkpointer), payment: PaymentClient = Depends(get_payment_client)):
    """Expire stale proposals and finish their paused workflows. Part 10's worker runs this on a schedule."""
    expired = approvals.expire_stale(db, _now())
    for pid in expired:
        resume_for_proposal(db, pid, "expired", checkpointer=checkpointer, payment=payment)
    return {"expired": expired}


@router.post("/proposals/{proposal_id}/execute", response_model=RefundOperationOut)
def execute(proposal_id: str, staff: Staff = Depends(get_current_staff), db: Session = Depends(get_db),
            payment: PaymentClient = Depends(get_payment_client)):
    return refunds.execute(db, staff.actor, proposal_id, payment, _now())


@router.post("/refunds/{operation_id}/refresh", response_model=RefundOperationOut)
def refresh(operation_id: str, staff: Staff = Depends(get_current_staff), db: Session = Depends(get_db),
            payment: PaymentClient = Depends(get_payment_client)):
    return refunds.refresh(db, staff.actor, operation_id, payment)
