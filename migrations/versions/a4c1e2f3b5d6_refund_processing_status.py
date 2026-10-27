"""add 'processing' refund status and 'awaiting_provider' ticket status

Revision ID: a4c1e2f3b5d6
Revises: 0177aa813749
Create Date: 2026-10-27 16:00:00

Rule 8 keeps submitted / processing / succeeded / failed distinct, and the
initial schema had no 'processing'. Postgres can't alter a CHECK in place,
so each constraint is dropped and recreated.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a4c1e2f3b5d6"
down_revision: Union[str, Sequence[str], None] = "0177aa813749"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_REFUND = ["reserved", "submitted", "succeeded", "failed", "pending_reconciliation"]
NEW_REFUND = ["reserved", "submitted", "processing", "succeeded", "failed", "pending_reconciliation"]
OLD_TICKET = ["open", "awaiting_customer", "awaiting_approval", "escalated", "resolved"]
NEW_TICKET = ["open", "awaiting_customer", "awaiting_approval", "awaiting_provider", "escalated", "resolved"]


def _in(values: list[str]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _replace(table: str, name: str, values: list[str]) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, _in(values))


def upgrade() -> None:
    _replace("refund_operations", "ck_refunds_status", NEW_REFUND)
    _replace("tickets", "ck_tickets_status", NEW_TICKET)


def downgrade() -> None:
    # Map new states onto their nearest old equivalents so the old CHECK can be restored.
    op.execute("UPDATE refund_operations SET status = 'submitted' WHERE status = 'processing'")
    op.execute("UPDATE tickets SET status = 'awaiting_approval' WHERE status = 'awaiting_provider'")
    _replace("refund_operations", "ck_refunds_status", OLD_REFUND)
    _replace("tickets", "ck_tickets_status", OLD_TICKET)
