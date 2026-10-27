"""record the agent's route and outcome on workflow_runs

Revision ID: b7d2f4a6c8e1
Revises: a4c1e2f3b5d6
Create Date: 2026-10-27 18:00:00

The route is chosen by code (Part 6); outcome keeps the triage, summary,
tool-trace digest, the model's own suggestion and whether code agreed with it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d2f4a6c8e1"
down_revision: Union[str, Sequence[str], None] = "a4c1e2f3b5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROUTES = ["propose_refund", "explain_policy", "report_order_status", "escalate", "order_not_found"]


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("route", sa.String(length=30), nullable=True))
    op.add_column("workflow_runs", sa.Column("route_reason", sa.String(length=100), nullable=True))
    op.add_column("workflow_runs", sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_check_constraint(
        "ck_runs_route", "workflow_runs",
        "route IS NULL OR route IN (" + ", ".join(f"'{r}'" for r in ROUTES) + ")")


def downgrade() -> None:
    op.drop_constraint("ck_runs_route", "workflow_runs", type_="check")
    op.drop_column("workflow_runs", "outcome")
    op.drop_column("workflow_runs", "route_reason")
    op.drop_column("workflow_runs", "route")
