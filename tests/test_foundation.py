"""Prove the database enforces the money rules, independent of app code."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.db.models import ActionProposal, RefundOperation, Ticket, WorkflowRun
from app.main import app


def test_health():
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["database"] == "ok"


def _proposal(db) -> ActionProposal:
    t = Ticket(customer_id="cus_asha", order_id="ord_1001", customer_message="refund?")
    db.add(t); db.flush()
    run = WorkflowRun(ticket_id=t.id, thread_id=f"thread-{t.id}")
    db.add(run); db.flush()
    p = ActionProposal(run_id=run.id, action_type="refund", payload={"amount_minor": 249900},
                       payload_hash="x" * 64)
    db.add(p); db.flush()
    return p


def test_duplicate_idempotency_key_rejected(db):
    p = _proposal(db)
    db.add(RefundOperation(order_id="ord_1001", proposal_id=p.id, amount_minor=249900, idempotency_key="key-1"))
    db.flush()
    db.add(RefundOperation(order_id="ord_1001", proposal_id=p.id, amount_minor=249900, idempotency_key="key-1"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_non_positive_refund_rejected(db):
    p = _proposal(db)
    db.add(RefundOperation(order_id="ord_1001", proposal_id=p.id, amount_minor=0, idempotency_key="key-2"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_unknown_status_rejected(db):
    p = _proposal(db)
    db.add(RefundOperation(order_id="ord_1001", proposal_id=p.id, amount_minor=100,
                           idempotency_key="key-3", status="probably_done"))
    with pytest.raises(IntegrityError):
        db.flush()
