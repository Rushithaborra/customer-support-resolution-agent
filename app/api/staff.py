"""Staff and reviewer routes. Operating a ticket needs a staff (or reviewer) token;
deciding a proposal needs a reviewer token, and never the proposal's creator."""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.agent.resume import request_resume, request_resume_superseded
from app.auth import Staff, get_current_reviewer, get_current_staff
from app.db.models import WorkflowRun
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


@router.get("/tickets", response_model=list[StaffTicketOut])
def list_tickets(status: Literal["open", "awaiting_customer", "awaiting_approval", "awaiting_provider", "escalated",
                                 "resolved"] | None = None,
                 _: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    """The support queue, newest first."""
    return [with_outcome(db, t, StaffTicketOut) for t in resolution.list_tickets(db, status)]


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
            embedder: Embedder = Depends(get_embedder)):
    proposal = resolution.propose_refund(db, staff, ticket_id, embedder)
    # Any agent proposal this one superseded: its paused workflow finishes as "invalidated" (worker).
    request_resume_superseded(db, ticket_id)
    db.commit()
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


def _decided(db: Session, response: Response, proposal_id: str, signal: str, approval=None) -> dict:
    """Queue the resume of a paused agent workflow. 202 when work was handed to the worker
    (the refund isn't done yet); 200 for a manual proposal, where the decision is everything."""
    job = request_resume(db, proposal_id, signal)
    db.commit()
    response.status_code = 202 if job else 200
    proposal = resolution.get_proposal(db, proposal_id)
    run_status = db.get(WorkflowRun, proposal.run_id).status if job else None  # agent workflows only
    return {"proposal": proposal, "approval": approval, "run_status": run_status,
            "refund": refunds.operation_for(db, proposal_id), "resume_job_id": job.id if job else None}


@router.post("/proposals/{proposal_id}/approve", response_model=DecisionOut)
def approve(proposal_id: str, body: ApproveIn, response: Response, reviewer: Staff = Depends(get_current_reviewer),
            db: Session = Depends(get_db)):
    """Store the approval. A paused agent workflow then resumes in the worker and executes the
    refund, so the provider is never called while this request is open."""
    approval = approvals.approve(db, reviewer, proposal_id, body.payload_hash, _now())
    return _decided(db, response, proposal_id, "approved", approval)


@router.post("/proposals/{proposal_id}/reject", response_model=DecisionOut)
def reject(proposal_id: str, response: Response, reviewer: Staff = Depends(get_current_reviewer),
           db: Session = Depends(get_db)):
    approvals.reject(db, reviewer, proposal_id, _now())
    return _decided(db, response, proposal_id, "rejected")


@router.post("/maintenance/expire-proposals", response_model=ExpiryOut)
def expire_proposals(_: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    """Expire stale proposals now (DB only) and queue the resumes of their paused workflows.
    The worker also runs this sweep every few minutes."""
    expired = approvals.expire_stale(db, _now())
    for pid in expired:
        request_resume(db, pid, "expired")
    db.commit()
    return {"expired": expired}


@router.post("/proposals/{proposal_id}/execute", response_model=RefundOperationOut)
def execute(proposal_id: str, staff: Staff = Depends(get_current_staff), db: Session = Depends(get_db),
            payment: PaymentClient = Depends(get_payment_client)):
    return refunds.execute(db, staff.actor, proposal_id, payment, _now())


@router.get("/refunds", response_model=list[RefundOperationOut])
def list_refunds(state: Literal["unsettled", "needs_review"] = "unsettled", _: Staff = Depends(get_current_staff),
                 db: Session = Depends(get_db)):
    """Refunds whose outcome isn't final. needs_review: the sweep gave up and a human must check."""
    return refunds.list_unsettled(db, needs_review=state == "needs_review")


@router.post("/refunds/{operation_id}/refresh", response_model=RefundOperationOut)
def refresh(operation_id: str, staff: Staff = Depends(get_current_staff), db: Session = Depends(get_db),
            payment: PaymentClient = Depends(get_payment_client)):
    return refunds.refresh(db, staff.actor, operation_id, payment)
