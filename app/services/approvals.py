"""Reviewer decisions and expiry. Each changes stored state only; resuming a
paused agent workflow afterwards is the caller's job (app/agent/resume.py).

Rules enforced here:
- Only a reviewer decides, and never on a proposal they created (four-eyes).
- The reviewer approves the exact payload they saw (hash sent back).
- Nothing waits forever: unreviewed proposals and unused approvals expire.
"""

from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.auth import REVIEWER_ROLE, Staff
from app.db.models import ActionProposal, Approval, AuditEvent, RefundOperation, Ticket, WorkflowRun
from app.domain.hashing import payload_hash
from app.domain.policy import APPROVAL_TTL, PROPOSAL_TTL
from app.services.errors import Conflict
from app.services.resolution import get_proposal


def _audit(db: Session, actor: str, event_type: str, proposal: ActionProposal, **extra) -> None:
    db.add(AuditEvent(run_id=proposal.run_id, actor=actor, event_type=event_type,
                      redacted_payload={"proposal_id": proposal.id, **extra}))


def _check_reviewer(reviewer: Staff, proposal: ActionProposal) -> None:
    if reviewer.role != REVIEWER_ROLE:  # defence in depth; the route already requires it
        raise Conflict("reviewer_required", "Only reviewers can decide proposals")
    if proposal.created_by == reviewer.subject:
        raise Conflict("self_approval", "A proposal can't be decided by the person who created it")
    if proposal.status != "pending_approval":
        raise Conflict("not_pending", f"Proposal is {proposal.status}")


def approve(db: Session, reviewer: Staff, proposal_id: str, presented_hash: str, now: datetime) -> Approval:
    """The reviewer sends back the hash of what they reviewed. If the proposal
    changed since, the hashes differ and the approval is refused."""
    proposal = get_proposal(db, proposal_id)
    _check_reviewer(reviewer, proposal)
    if presented_hash != proposal.payload_hash:
        raise Conflict("payload_hash_mismatch", "The proposal changed since it was reviewed")
    if payload_hash(proposal.payload) != proposal.payload_hash:
        raise Conflict("payload_tampered", "Stored payload no longer matches its hash")

    approval = Approval(proposal_id=proposal.id, approved_payload_hash=proposal.payload_hash,
                        reviewer_id=reviewer.subject, decision="approved",
                        expires_at=now + APPROVAL_TTL, decided_at=now)
    db.add(approval)
    proposal.status = "approved"
    _audit(db, reviewer.actor, "proposal.approved", proposal, approved_payload_hash=proposal.payload_hash)
    db.commit()
    return approval


def reject(db: Session, reviewer: Staff, proposal_id: str, now: datetime) -> ActionProposal:
    proposal = get_proposal(db, proposal_id)
    _check_reviewer(reviewer, proposal)
    db.add(Approval(proposal_id=proposal.id, approved_payload_hash=proposal.payload_hash,
                    reviewer_id=reviewer.subject, decision="rejected", expires_at=now, decided_at=now))
    proposal.status = "rejected"
    run = db.get(WorkflowRun, proposal.run_id)
    run.status, run.completed_at = "completed", now
    db.get(Ticket, run.ticket_id).status = "open"
    _audit(db, reviewer.actor, "proposal.rejected", proposal)
    db.commit()
    return proposal


def expire_stale(db: Session, now: datetime) -> list[str]:
    """Expire (a) proposals nobody reviewed within PROPOSAL_TTL and (b) approved
    proposals whose approval ran out before execution started. Their tickets go
    to a human. Returns the expired proposal ids."""
    no_refund_started = ~exists().where(RefundOperation.proposal_id == ActionProposal.id)
    unreviewed = select(ActionProposal).where(ActionProposal.status == "pending_approval",
                                              ActionProposal.created_at <= now - PROPOSAL_TTL)
    unused = (select(ActionProposal).join(Approval, Approval.proposal_id == ActionProposal.id)
              .where(ActionProposal.status == "approved", Approval.decision == "approved",
                     Approval.expires_at <= now, no_refund_started))
    expired = []
    for proposal in list(db.scalars(unreviewed).all()) + list(db.scalars(unused).unique().all()):
        if proposal.status == "expired":
            continue
        proposal.status = "expired"
        run = db.get(WorkflowRun, proposal.run_id)
        run.status, run.completed_at = "escalated", now
        ticket = db.get(Ticket, run.ticket_id)
        if ticket.status == "awaiting_approval":
            ticket.status = "escalated"
        _audit(db, "system", "proposal.expired", proposal)
        expired.append(proposal.id)
    db.commit()
    return expired
