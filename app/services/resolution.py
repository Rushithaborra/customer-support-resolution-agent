"""Staff-driven resolution: classify, assess, propose, approve/reject, close.

Every decision here is code. Staff choose *which* step to take; they cannot
choose the amount, the eligibility outcome, or who they are."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Staff
from app.db.models import ActionProposal, Approval, AuditEvent, Ticket, WorkflowRun
from app.domain.eligibility import assess_damage, evaluate_refund
from app.domain.hashing import payload_hash
from app.domain.policy import APPROVAL_TTL, POLICY_VERSION
from app.integrations.results import Ok
from app.integrations.shipping import ShippingClient
from app.policy.embedder import Embedder
from app.policy.queries import query_for
from app.policy.retrieve import retrieve
from app.services.errors import Conflict, NotEligible, NotFound
from app.services.facts import load_order_facts
from app.services.proposals import build_refund_payload, create_refund_proposal

# Ticket states where money may be in flight: closing by hand would hide it.
_IN_FLIGHT_TICKET_STATES = {"awaiting_approval", "awaiting_provider"}


def _audit(db: Session, actor: str, event_type: str, run_id: str | None = None, **payload) -> None:
    db.add(AuditEvent(run_id=run_id, actor=actor, event_type=event_type, redacted_payload=payload))


def get_ticket(db: Session, ticket_id: str) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFound("Ticket")
    return ticket


def get_proposal(db: Session, proposal_id: str) -> ActionProposal:
    proposal = db.get(ActionProposal, proposal_id)
    if proposal is None:
        raise NotFound("Proposal")
    return proposal


def classify(db: Session, staff: Staff, ticket_id: str, category: str) -> Ticket:
    ticket = get_ticket(db, ticket_id)
    previous, ticket.category = ticket.category, category
    _audit(db, staff.actor, "ticket.classified", ticket_id=ticket.id, previous=previous, category=category)
    db.commit()
    return ticket


def assess(db: Session, ticket_id: str, shipping: ShippingClient, now: datetime) -> dict:
    """Read-only: facts, rule outcomes and evidence for the reviewer."""
    ticket = get_ticket(db, ticket_id)
    result: dict = {"ticket_id": ticket.id, "category": ticket.category, "order_id": ticket.order_id,
                    "policy_version": POLICY_VERSION, "refund": None, "damage": None,
                    "shipment": None, "shipment_error": None, "evidence": None}
    if ticket.order_id is None:
        return result

    facts = load_order_facts(db, ticket.order_id)
    db.commit()  # end the read transaction before any network call
    refund = evaluate_refund(facts)
    damage = assess_damage(facts, now)
    r = facts.latest_return
    result.update(
        refund={"eligible": refund.eligible, "reason_code": refund.reason_code,
                "refundable_minor": refund.refundable_minor},
        damage={"recommendation": damage.recommendation, "reason_code": damage.reason_code},
        evidence={
            "order_status": facts.status, "paid_amount_minor": facts.paid_amount_minor,
            "currency": facts.currency, "delivered_at": facts.delivered_at,
            "held_refund_minor": facts.held_minor,
            "return": None if r is None else {"return_id": r.return_id, "status": r.status,
                                              "inspection_status": r.inspection_status,
                                              "received_at": r.received_at},
        },
    )
    if ticket.category == "order_status":
        shipment = shipping.get_shipment(ticket.order_id)
        if isinstance(shipment, Ok):
            result["shipment"] = shipment.value.model_dump(mode="json")
        else:
            result["shipment_error"] = "shipping_unavailable"
    return result


def propose_refund(db: Session, staff: Staff, ticket_id: str, embedder: Embedder) -> ActionProposal:
    """Code decides eligibility, computes the amount and finds the policy to cite; staff can only ask."""
    ticket = get_ticket(db, ticket_id)
    if ticket.order_id is None:
        raise NotEligible("no_order")
    facts = load_order_facts(db, ticket.order_id)
    decision = evaluate_refund(facts)
    if not decision.eligible:
        raise NotEligible(decision.reason_code)
    citations = retrieve(db, query_for(decision.reason_code), embedder=embedder, as_of=datetime.now(timezone.utc))
    if not citations:
        raise NotEligible("no_policy_found")

    payload = build_refund_payload(
        ticket.id, facts, decision,
        evidence_refs=[f"code:evaluate_refund:{facts.order_id}", f"db:return:{facts.latest_return.return_id}"],
        proposed_by=staff.actor, rationale="Proposed by staff after manual assessment",
        policy_citations=[c.ref for c in citations])
    run = WorkflowRun(ticket_id=ticket.id, thread_id=f"manual-{uuid.uuid4().hex}", status="paused")
    db.add(run)
    db.flush()
    proposal = create_refund_proposal(db, run=run, ticket=ticket, payload=payload, actor=staff.actor)
    db.commit()
    return proposal


def list_proposals(db: Session, status: str | None) -> list[ActionProposal]:
    q = select(ActionProposal).order_by(ActionProposal.created_at.desc())
    if status:
        q = q.where(ActionProposal.status == status)
    return list(db.scalars(q).all())


def approve(db: Session, staff: Staff, proposal_id: str, presented_hash: str, now: datetime) -> Approval:
    """The reviewer sends back the hash of what they reviewed. If the proposal
    changed since, the hashes differ and the approval is refused."""
    proposal = get_proposal(db, proposal_id)
    if proposal.status != "pending_approval":
        raise Conflict("not_pending", f"Proposal is {proposal.status}")
    if presented_hash != proposal.payload_hash:
        raise Conflict("payload_hash_mismatch", "The proposal changed since it was reviewed")
    if payload_hash(proposal.payload) != proposal.payload_hash:
        raise Conflict("payload_tampered", "Stored payload no longer matches its hash")

    approval = Approval(proposal_id=proposal.id, approved_payload_hash=proposal.payload_hash,
                        reviewer_id=staff.subject, decision="approved",
                        expires_at=now + APPROVAL_TTL, decided_at=now)
    db.add(approval)
    proposal.status = "approved"
    _audit(db, staff.actor, "proposal.approved", run_id=proposal.run_id, proposal_id=proposal.id,
           approved_payload_hash=proposal.payload_hash)
    db.commit()
    return approval


def reject(db: Session, staff: Staff, proposal_id: str, now: datetime) -> ActionProposal:
    proposal = get_proposal(db, proposal_id)
    if proposal.status != "pending_approval":
        raise Conflict("not_pending", f"Proposal is {proposal.status}")
    db.add(Approval(proposal_id=proposal.id, approved_payload_hash=proposal.payload_hash,
                    reviewer_id=staff.subject, decision="rejected", expires_at=now, decided_at=now))
    proposal.status = "rejected"
    run = db.get(WorkflowRun, proposal.run_id)
    run.status, run.completed_at = "completed", now
    db.get(Ticket, run.ticket_id).status = "open"
    _audit(db, staff.actor, "proposal.rejected", run_id=run.id, proposal_id=proposal.id)
    db.commit()
    return proposal


def close_ticket(db: Session, staff: Staff, ticket_id: str, outcome: str, summary: str) -> Ticket:
    """Non-money outcomes: order-status answered, ineligible explained, damage escalated."""
    ticket = get_ticket(db, ticket_id)
    if ticket.status in _IN_FLIGHT_TICKET_STATES:
        raise Conflict("refund_in_flight", f"Ticket is {ticket.status}")
    ticket.status, ticket.summary = outcome, summary
    _audit(db, staff.actor, "ticket.closed", ticket_id=ticket.id, outcome=outcome)
    db.commit()
    return ticket
