"""proposal creator (four-eyes) and durable LangGraph checkpoint tables

Revision ID: d4f6b8c0e2a3
Revises: c3e5a7b9d1f2
Create Date: 2026-10-28 10:00:00

- action_proposals.created_by: "agent" or the staff subject who proposed it.
  A reviewer may never approve a proposal they created.
- langgraph schema: LangGraph's checkpoint tables, created by the library's own
  versioned setup() so `alembic upgrade head` stays the single setup step. They
  hold workflow position only, never money state.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "d4f6b8c0e2a3"
down_revision: Union[str, Sequence[str], None] = "c3e5a7b9d1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("action_proposals", sa.Column("created_by", sa.String(length=64), nullable=True))
    from app.agent.checkpoint import ensure_checkpoint_tables
    ensure_checkpoint_tables(context.config.get_main_option("sqlalchemy.url"))


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")
    op.drop_column("action_proposals", "created_by")
