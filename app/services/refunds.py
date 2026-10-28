"""Refund execution and reconciliation (CLAUDE.md rule 5).

  validate approval ─► reserve (FOR UPDATE, short txn, commit) ─► mark submitted (commit)
  ─► provider call with NO transaction open ─► record result (short txn, commit)

The idempotency key is derived from the proposal, so every retry of this
proposal (by a person, a worker, or after a crash) presents the same key and
the provider can apply it at most once. UNIQUE on the key means we can't
create a second operation for the proposal either.

Part 11: nothing waits for a person to click "refresh". Every op that isn't final
carries next_reconcile_at, and the worker's reconcile sweep asks the provider about
it with backoff. An outcome that stays unknown is parked for a human, never marked
failed, because the money may have moved.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ActionProposal, Approval, AuditEvent, RefundOperation, Ticket, WorkflowRun
from app.domain.eligibility import evaluate_refund
from app.domain.hashing import payload_hash
from app.integrations.payment import PaymentClient, ProviderRefund
from app.integrations.results import Ok, Rejected, Result, Retryable, UnknownOutcome
from app.services.errors import Conflict, NotEligible, NotFound, ProviderUnavailable
from app.services.facts import load_order_facts

FINAL = {"succeeded", "failed"}

# The sender is mid-call for up to its client timeout; the sweep waits this long before asking.
RECONCILE_GRACE = timedelta(seconds=60)
MAX_RECONCILE_ATTEMPTS = 8  # about 3 hours of backoff, then a human looks


def reconcile_backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * 2 ** attempts, 3600))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def idempotency_key_for(proposal_id: str) -> str:
    return f"refund-{proposal_id}"


def _audit(db: Session, actor: str, event_type: str, op: RefundOperation, run_id: str, **extra) -> None:
    db.add(AuditEvent(run_id=run_id, actor=actor, event_type=event_type,
                      redacted_payload={"refund_operation_id": op.id, "status": op.status, **extra}))


def operation_for(db: Session, proposal_id: str) -> RefundOperation | None:
    return db.scalar(select(RefundOperation).where(RefundOperation.proposal_id == proposal_id))


# ---------------------------------------------------------------- entry points


def execute(db: Session, actor: str, proposal_id: str, payment: PaymentClient,
            now: datetime | None = None) -> RefundOperation:
    """Idempotent: calling it again continues the existing operation, never starts a second."""
    now = now or datetime.now(timezone.utc)
    proposal = db.get(ActionProposal, proposal_id)
    if proposal is None:
        raise NotFound("Proposal")

    op = operation_for(db, proposal.id)
    if op is not None:
        if op.status in FINAL:
            return op
        if op.status == "reserved":
            _validate_approval(db, proposal, now)
            return _send(db, actor, op, proposal, payment)
        return refresh(db, actor, op.id, payment)

    _validate_approval(db, proposal, now)
    op = _reserve(db, actor, proposal)
    if op.status != "reserved":  # a concurrent execute got there first
        return refresh(db, actor, op.id, payment) if op.status not in FINAL else op
    return _send(db, actor, op, proposal, payment)


def refresh(db: Session, actor: str, operation_id: str, payment: PaymentClient,
            now: datetime | None = None) -> RefundOperation:
    """Reconcile with the provider. Never guesses: it asks, and only resends
    (with the same key) when the provider says it has no such refund."""
    op = db.get(RefundOperation, operation_id, populate_existing=True)
    if op is None:
        raise NotFound("Refund")
    if op.status in FINAL:
        return op
    proposal = db.get(ActionProposal, op.proposal_id)
    if op.status == "reserved":
        # Never sent; sending needs a still-valid approval.
        _validate_approval(db, proposal, now or _utcnow())
        return _send(db, actor, op, proposal, payment)
    db.commit()  # end the read transaction before the network call

    if op.status == "processing" and op.provider_reference:
        lookup: Result = payment.get_refund(op.provider_reference)
    else:
        lookup = payment.find_refund_by_key(op.idempotency_key)

    if not isinstance(lookup, Ok):
        # Couldn't learn anything: change nothing.
        raise ProviderUnavailable(f"reconciliation lookup failed: {lookup}")
    if lookup.value is None:
        # Provider has no refund under this key, so it never happened. Resending with
        # the SAME key is safe: if a delayed original lands first, this replays it.
        return _record(db, actor, op, proposal, payment.create_refund(
            order_id=op.order_id, amount_minor=op.amount_minor,
            idempotency_key=op.idempotency_key, currency=proposal.payload["currency"]))
    return _record(db, actor, op, proposal, lookup)


# ---------------------------------------------------------------- steps


def _validate_approval(db: Session, proposal: ActionProposal, now: datetime) -> Approval:
    if proposal.status != "approved":
        raise Conflict("not_approved", f"Proposal is {proposal.status}")
    approval = db.scalar(
        select(Approval).where(Approval.proposal_id == proposal.id, Approval.decision == "approved")
        .order_by(Approval.decided_at.desc()).limit(1)
    )
    if approval is None:
        raise Conflict("not_approved", "No stored approval")
    if approval.expires_at <= now:
        proposal.status = "expired"
        db.add(AuditEvent(run_id=proposal.run_id, actor="system", event_type="proposal.expired",
                          redacted_payload={"proposal_id": proposal.id}))
        db.commit()
        raise Conflict("approval_expired", "Approval expired; a fresh approval is required")
    # The approval covers exactly this payload: the hash it approved, the hash on the
    # proposal, and the hash of the payload as stored right now must all agree.
    if not (approval.approved_payload_hash == proposal.payload_hash == payload_hash(proposal.payload)):
        raise Conflict("approval_does_not_match_payload", "Payload changed after approval")
    return approval


def _reserve(db: Session, actor: str, proposal: ActionProposal) -> RefundOperation:
    """Short transaction: lock the order, re-check the rules on fresh facts, hold the amount."""
    payload = proposal.payload
    facts = load_order_facts(db, payload["order_id"], lock=True)
    decision = evaluate_refund(facts)
    amount = payload["amount_minor"]
    if not decision.eligible:
        db.rollback()
        raise NotEligible(decision.reason_code)
    if amount > decision.refundable_minor or payload["currency"] != facts.currency:
        db.rollback()
        raise NotEligible("exceeds_refundable")

    op = RefundOperation(order_id=facts.order_id, proposal_id=proposal.id, amount_minor=amount,
                         idempotency_key=idempotency_key_for(proposal.id), status="reserved",
                         next_reconcile_at=_utcnow() + RECONCILE_GRACE)  # a crash before sending is found too
    db.add(op)
    try:
        db.flush()
    except IntegrityError:
        # Same key already exists: another execute of this proposal won the race.
        db.rollback()
        return operation_for(db, proposal.id)
    _audit(db, actor, "refund.reserved", op, proposal.run_id, amount_minor=amount)
    db.commit()  # releases the row lock
    return op


def _send(db: Session, actor: str, op: RefundOperation, proposal: ActionProposal,
          payment: PaymentClient) -> RefundOperation:
    op.status = "submitted"
    # Scheduled in the same commit: if we die mid-call, the sweep finds this op.
    op.next_reconcile_at = _utcnow() + RECONCILE_GRACE
    _audit(db, actor, "refund.submitted", op, proposal.run_id)
    db.commit()
    # From here until _record, no transaction is open: a slow provider can't hold locks.
    result = payment.create_refund(order_id=op.order_id, amount_minor=op.amount_minor,
                                   idempotency_key=op.idempotency_key,
                                   currency=proposal.payload["currency"])
    return _record(db, actor, op, proposal, result)


def _record(db: Session, actor: str, op: RefundOperation, proposal: ActionProposal,
            result: Result[ProviderRefund]) -> RefundOperation:
    if isinstance(result, Retryable):
        # Nothing was applied, but a request may have been in flight (e.g. 409 in progress),
        # so the status stays where it is and reconciliation will ask again.
        _audit(db, actor, "refund.provider_unavailable", op, proposal.run_id, reason=result.reason)
        _sync_workflow(db, op, proposal)  # ticket shows awaiting_provider, not "awaiting approval"
        db.commit()
        raise ProviderUnavailable(result.reason)

    if isinstance(result, UnknownOutcome):
        op.status = "pending_reconciliation"  # never 'failed': money may have moved
        _audit(db, actor, "refund.outcome_unknown", op, proposal.run_id, reason=result.reason)
    elif isinstance(result, Rejected):
        op.status = "failed"
        _audit(db, actor, "refund.rejected", op, proposal.run_id, code=result.code)
    else:
        refund: ProviderRefund = result.value
        if refund.amount_minor != op.amount_minor or refund.order_id != op.order_id:
            # Verify, don't trust: a provider record that doesn't match ours needs a human.
            op.status = "pending_reconciliation"
            _audit(db, actor, "refund.provider_mismatch", op, proposal.run_id,
                   provider_amount_minor=refund.amount_minor)
            _sync_workflow(db, op, proposal)
            _park(db, op, proposal, "provider_mismatch")
            db.commit()
            return op
        else:
            op.status = refund.status  # processing | succeeded | failed
            op.provider_reference = refund.refund_id
            _audit(db, actor, f"refund.{refund.status}", op, proposal.run_id,
                   provider_reference=refund.refund_id, replayed=result.replayed)

    if op.status in FINAL:
        op.next_reconcile_at, op.needs_review_reason = None, None  # settled: nothing left to ask
    _sync_workflow(db, op, proposal)
    db.commit()
    return op


# ---------------------------------------------------------------- reconciliation sweep


def list_unsettled(db: Session, *, needs_review: bool) -> list[RefundOperation]:
    q = select(RefundOperation).where(RefundOperation.status.notin_(FINAL))
    q = q.where(RefundOperation.needs_review_reason.is_not(None) if needs_review
                else RefundOperation.needs_review_reason.is_(None))
    return list(db.scalars(q.order_by(RefundOperation.created_at)).all())


def reconcile_due(db: Session, payment: PaymentClient, now: datetime | None = None, limit: int = 20) -> list[str]:
    """Ask the provider about every op that is due. Returns the ids asked about.

    Pick in one short transaction (SKIP LOCKED, so parallel sweeps split the work and
    each op's next slot is booked before any network call), then reconcile each op
    with no transaction open."""
    now = now or _utcnow()
    due = db.scalars(
        select(RefundOperation)
        .where(RefundOperation.next_reconcile_at <= now, RefundOperation.needs_review_reason.is_(None),
               RefundOperation.status.notin_(FINAL))
        .order_by(RefundOperation.next_reconcile_at).limit(limit).with_for_update(skip_locked=True)
    ).all()
    picked = []
    for op in due:
        if op.reconcile_attempts >= MAX_RECONCILE_ATTEMPTS:
            _park(db, op, db.get(ActionProposal, op.proposal_id), "reconcile_exhausted")
            continue
        op.reconcile_attempts += 1
        op.next_reconcile_at = now + reconcile_backoff(op.reconcile_attempts)
        picked.append(op.id)
    db.commit()

    for op_id in picked:
        try:
            refresh(db, "system", op_id, payment, now)
        except ProviderUnavailable:
            db.rollback()  # learned nothing; the booked slot asks again later
        except Conflict:
            db.rollback()
            _cancel_unsent(db, op_id)
    return picked


def _cancel_unsent(db: Session, op_id: str) -> None:
    """A 'reserved' op was never sent (status flips to 'submitted' before any call), but
    its approval is gone. Release the held balance; the ticket goes back to a human."""
    op = db.get(RefundOperation, op_id, populate_existing=True)
    if op.status != "reserved":
        return
    proposal = db.get(ActionProposal, op.proposal_id)
    op.status, op.next_reconcile_at = "failed", None
    _audit(db, "system", "refund.cancelled_unsent", op, proposal.run_id)
    run = db.get(WorkflowRun, proposal.run_id)
    run.status, run.completed_at = "escalated", _utcnow()
    db.get(Ticket, run.ticket_id).status = "escalated"
    db.commit()


def _park(db: Session, op: RefundOperation, proposal: ActionProposal, reason: str) -> None:
    """Stop asking automatically. The status stays as it is: unknown is never 'failed'."""
    op.needs_review_reason, op.next_reconcile_at = reason, None
    _audit(db, "system", "refund.needs_review", op, proposal.run_id, reason=reason)
    run = db.get(WorkflowRun, proposal.run_id)
    db.get(Ticket, run.ticket_id).status = "escalated"


def _sync_workflow(db: Session, op: RefundOperation, proposal: ActionProposal) -> None:
    """Ticket, run and proposal follow the refund's confirmed state."""
    run = db.get(WorkflowRun, proposal.run_id)
    ticket = db.get(Ticket, run.ticket_id)
    now = datetime.now(timezone.utc)
    if op.status == "succeeded":
        proposal.status, run.status, run.completed_at, ticket.status = "executed", "completed", now, "resolved"
    elif op.status == "failed":
        proposal.status, run.status, run.completed_at, ticket.status = "executed", "failed", now, "escalated"
    elif op.status == "processing":
        proposal.status, ticket.status = "executed", "awaiting_provider"
    else:  # submitted / pending_reconciliation: outcome not yet known
        ticket.status = "awaiting_provider"
