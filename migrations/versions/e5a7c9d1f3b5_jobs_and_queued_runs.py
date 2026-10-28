"""background job queue and the queued run status

Revision ID: e5a7c9d1f3b5
Revises: d4f6b8c0e2a3
Create Date: 2026-10-28 12:00:00

- jobs: durable work queue claimed with FOR UPDATE SKIP LOCKED, with a lease
  (locked_until) so a crashed worker's job is taken over by another.
- workflow_runs.status gains "queued": the request recorded the run, no worker has it yet.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a7c9d1f3b5"
down_revision: Union[str, Sequence[str], None] = "d4f6b8c0e2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_RUN_STATUSES = ["running", "paused", "completed", "failed", "escalated"]
NEW_RUN_STATUSES = ["queued", *OLD_RUN_STATUSES]


def _in(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("run_id", sa.String(length=32), sa.ForeignKey("workflow_runs.id"), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("dedupe_key", sa.String(length=128), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('agent_run', 'resume', 'expire_sweep')", name="ck_jobs_kind"),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'dead')", name="ck_jobs_status"),
    )
    op.create_index("ix_jobs_claimable", "jobs", ["status", "run_after"])
    op.create_index("ix_jobs_run_id", "jobs", ["run_id"])

    op.drop_constraint("ck_runs_status", "workflow_runs", type_="check")
    op.create_check_constraint("ck_runs_status", "workflow_runs", f"status IN ({_in(NEW_RUN_STATUSES)})")


def downgrade() -> None:
    op.execute("UPDATE workflow_runs SET status = 'failed' WHERE status = 'queued'")
    op.drop_constraint("ck_runs_status", "workflow_runs", type_="check")
    op.create_check_constraint("ck_runs_status", "workflow_runs", f"status IN ({_in(OLD_RUN_STATUSES)})")
    op.drop_index("ix_jobs_run_id", table_name="jobs")
    op.drop_index("ix_jobs_claimable", table_name="jobs")
    op.drop_table("jobs")
