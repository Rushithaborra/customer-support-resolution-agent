"""Durable LangGraph checkpoints in Postgres.

They live in their own `langgraph` schema: a workflow cursor ("this run is
paused at await_approval"), not the financial ledger. Money state lives only
in refund_operations / action_proposals (CLAUDE.md rule 6).
"""

from functools import lru_cache

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

CHECKPOINT_SCHEMA = "langgraph"


def conninfo(database_url: str | None = None) -> str:
    # SQLAlchemy URL → plain libpq URL for psycopg.
    return (database_url or settings.database_url).replace("postgresql+psycopg://", "postgresql://")


def ensure_checkpoint_tables(database_url: str | None = None) -> None:
    """Idempotent: create the schema and run the library's own migrations."""
    with psycopg.connect(conninfo(database_url), autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {CHECKPOINT_SCHEMA}")
    saver, pool = _open(database_url, max_size=1)
    try:
        saver.setup()
    finally:
        pool.close()


def _open(database_url: str | None = None, max_size: int = 4) -> tuple[PostgresSaver, ConnectionPool]:
    pool = ConnectionPool(
        conninfo(database_url), min_size=1, max_size=max_size, open=True,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                "options": f"-c search_path={CHECKPOINT_SCHEMA}"})
    return PostgresSaver(pool), pool


def open_checkpointer() -> tuple[PostgresSaver, ConnectionPool]:
    """A fresh saver with its own connection pool. Close the pool when done."""
    return _open()


@lru_cache(maxsize=1)
def get_checkpointer() -> PostgresSaver:
    """FastAPI dependency: one saver (and pool) per process."""
    saver, _ = _open()
    return saver
