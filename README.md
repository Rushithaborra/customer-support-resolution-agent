# Customer Support Resolution Agent

AI agent that takes a customer complaint from "what happened?" to a verified outcome,
using business data, versioned policies, controlled tools and human approval.

**Status:** Part 1 — foundation (schema, migrations, seed data, health check).

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
uv run pytest -q                     # verify DB constraints
uv run uvicorn app.main:app --reload # http://127.0.0.1:8000/docs
```
