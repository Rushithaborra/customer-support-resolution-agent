# Customer Support Resolution Agent

AI agent that takes a customer complaint from "what happened?" to a verified outcome,
using business data, versioned policies, controlled tools and human approval.

**Status:** Parts 1–3 done: foundation, token auth with ownership checks, and mock
external services with an idempotency contract. See `docs/project-overview.md`.

## Scope (v1)
One store, one currency (INR). Three complaint types: order status, refund request,
damaged item. Every refund requires human approval. Payment and shipping are mocked.
Excluded: chargebacks, fraud, partial returns. All data is synthetic.

## Setup
```bash
cp .env.example .env
docker compose up -d                 # Postgres 16 + pgvector
uv sync                              # install pinned deps from uv.lock
uv run alembic upgrade head          # create schema
uv run python -m scripts.seed_data   # load scenario data
uv run pytest -q                     # full suite (starts its own mock services)
uv run uvicorn app.main:app --reload # http://127.0.0.1:8000/docs
uv run uvicorn mocks.main:app --port 8001  # mock payment/shipping/helpdesk, http://127.0.0.1:8001/docs
```
