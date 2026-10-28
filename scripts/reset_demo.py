"""Reset everything a demo touched, so the next take starts clean. Synthetic data only.

- Database: re-seed (wipes tickets, runs, proposals, approvals, refunds, jobs, audit; recreates the
  synthetic customers and orders with fresh relative dates) and clear LangGraph checkpoints.
- Mock provider: clear its refunds and injected faults, if the mocks are running.

Run:  uv run python -m scripts.reset_demo
"""

import psycopg

from app.agent.checkpoint import CHECKPOINT_SCHEMA, conninfo
from app.integrations.http import ServiceClient
from app.integrations.results import Ok
from scripts import seed_data


def clear_checkpoints() -> None:
    with psycopg.connect(conninfo(), autocommit=True) as conn:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            conn.execute(f"DELETE FROM {CHECKPOINT_SCHEMA}.{table}")


def reset_mocks() -> bool:
    with ServiceClient(connect_timeout_s=1, read_timeout_s=2) as http:
        return isinstance(http.call("POST", "/_admin/reset"), Ok)


def main() -> None:
    seed_data.main()
    clear_checkpoints()
    print("Cleared workflow checkpoints.")
    print("Mock provider reset." if reset_mocks() else "Mocks not running (nothing to reset there).")


if __name__ == "__main__":
    main()
