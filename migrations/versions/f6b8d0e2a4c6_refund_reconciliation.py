"""automatic refund reconciliation: schedule columns and the reconcile_sweep job kind

Revision ID: f6b8d0e2a4c6
Revises: e5a7c9d1f3b5
Create Date: 2026-10-28 15:00:00

- refund_operations.reconcile_attempts / next_reconcile_at: when the sweep should next ask
  the provider about an op whose outcome isn't final.
- refund_operations.needs_review_reason: parked for a human (provider mismatch, or the
  provider never gave an answer); the sweep leaves it alone.
- jobs.kind gains reconcile_sweep.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6b8d0e2a4c6"
down_revision: Union[str, Sequence[str], None] = "e5a7c9d1f3b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_KINDS = ["agent_run", "resume", "expire_sweep"]
NEW_KINDS = [*OLD_KINDS, "reconcile_sweep"]


def _in(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.add_column("refund_operations", sa.Column("reconcile_attempts", sa.Integer(), nullable=False,
                                                 server_default="0"))
    op.alter_column("refund_operations", "reconcile_attempts", server_default=None)
    op.add_column("refund_operations", sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("refund_operations", sa.Column("needs_review_reason", sa.String(length=64), nullable=True))
    op.create_index("ix_refund_operations_next_reconcile_at", "refund_operations", ["next_reconcile_at"])
    # Anything already stuck gets looked at on the first sweep.
    op.execute("UPDATE refund_operations SET next_reconcile_at = now() WHERE status NOT IN ('succeeded', 'failed')")

    op.drop_constraint("ck_jobs_kind", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_kind", "jobs", f"kind IN ({_in(NEW_KINDS)})")


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'reconcile_sweep'")
    op.drop_constraint("ck_jobs_kind", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_kind", "jobs", f"kind IN ({_in(OLD_KINDS)})")
    op.drop_index("ix_refund_operations_next_reconcile_at", table_name="refund_operations")
    op.drop_column("refund_operations", "needs_review_reason")
    op.drop_column("refund_operations", "next_reconcile_at")
    op.drop_column("refund_operations", "reconcile_attempts")
