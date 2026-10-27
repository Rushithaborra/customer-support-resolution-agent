"""Migrations are reversible and the database matches the models."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from app.db.models import RefundOperation, Ticket


def test_downgrade_then_upgrade_round_trips():
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")
    command.check(cfg)  # raises if models and schema have drifted
    # The round trip drops and re-adds the embedding column; restore the reference data.
    from app.db.session import SessionLocal
    from app.policy.embedder import get_embedder
    from app.policy.ingest import ingest_dir
    with SessionLocal() as s:
        assert any(ingest_dir(s, get_embedder()).values())


def test_new_statuses_accepted(db):
    t = Ticket(customer_id="cus_asha", customer_message="x", status="awaiting_provider")
    db.add(t)
    db.flush()


def test_unknown_ticket_status_still_rejected(db):
    db.add(Ticket(customer_id="cus_asha", customer_message="x", status="kind_of_done"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_processing_refund_status_exists():
    from app.db.models import REFUND_STATUSES
    assert "processing" in REFUND_STATUSES
    assert RefundOperation.__table__.c.status is not None
