# Customer Support Resolution Agent

AI agent that takes a customer complaint from "what happened?" to a verified outcome,
using business data, versioned policies, controlled tools and human approval.

**Status:** Parts 1–4 done: foundation, token auth with ownership checks, mock external
services with an idempotency contract, and a full manual (no-LLM) resolution path: eligibility
rules in code, hash-bound approvals, and exactly-once refund execution. Part 5 adds the LLM
(Anthropic) behind read-only typed tools and validated structured outputs. Part 6 wires them into a
LangGraph workflow per ticket where the model classifies and investigates but code picks the route.
Part 7 lets the agent draft refund proposals with evidence refs; code rejects any draft whose amount,
order or evidence doesn't match the facts and the run's own tool trace. Part 8 adds versioned policy
documents with local embeddings (bge-small-en-v1.5) in pgvector; every decision cites the policy
version it applied, and if no policy applies the ticket escalates. Part 9 makes approval durable:
the workflow pauses with LangGraph `interrupt()` (checkpoints in Postgres), a separate reviewer role
approves (never the proposer), stale proposals expire, and the run resumes into exactly-once
execution, even after a restart. Part 10 makes runs async: requests return `202 Accepted` and a
worker claims jobs from a Postgres queue (`FOR UPDATE SKIP LOCKED`, leases, retries); a run whose
worker dies is taken over and continues from its last checkpoint. Part 11 reconciles every
refund whose outcome is unknown automatically (a sweep asks the provider by idempotency key, with
backoff, and parks it for a human if it never gets an answer) and proves zero duplicate refunds
across timeouts, crashes and concurrent executions. Part 12 adds a browser UI (customer panel,
staff/reviewer console with workflow timeline, evidence and approval queue) where Demos A, B and C
run. Part 13 measures it: 60 synthetic cases with a held-out split, a single-agent vs
specialist comparison, and a report reproducible from one command. See `docs/project-overview.md`.

## Run the demo
Four terminals (Postgres running, seeded, policies ingested):
```bash
uv run uvicorn mocks.main:app --port 8001                                   # mock payment/shipping
DEV_MODE=true LLM_PROVIDER=demo uv run uvicorn app.main:app --port 8000      # API + UI
DEV_MODE=true LLM_PROVIDER=demo uv run python -m scripts.worker              # background worker
open http://127.0.0.1:8000/ui/                                               # pick a persona
```
`LLM_PROVIDER=demo` is a deterministic keyword-rule stand-in (not an AI) so demos repeat exactly;
use `ollama` or `anthropic` for a real model. `DEV_MODE` enables the persona login and fault buttons;
never enable it outside local development. Reset between demos with `uv run python -m scripts.seed_data`.
The three demos are described on the start page and in `docs/parts/part-12-frontend.md`.

## Evaluation
60 synthetic cases (40 dev / 20 held-out) run through the real agent graph, graded
deterministically from stored state (route, reason, proposal amount, unsafe proposals, leaks):
```bash
uv run python -m evals.run --model demo --mode single,specialist      # seconds
uv run python -m evals.run --model ollama --mode single,specialist --resume   # hours on a laptop
uv run python -m evals.report                                         # rebuild from saved results
```
Results: `evals/reports/report.md`. Synthetic data only; not production performance.

Optional, to try the agent for real: put `ANTHROPIC_API_KEY` in `.env` (or, free and local, set
`LLM_PROVIDER=ollama` after `ollama pull qwen2.5:7b`), start the mocks, then
`uv run python -m scripts.llm_smoke "I returned my headphones but no refund yet"`.

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
uv run python -m scripts.ingest_policies  # embed policies/*.md (first run downloads ~64 MB model)
uv run pytest -q                     # full suite (starts its own mock services)
uv run uvicorn app.main:app --reload # http://127.0.0.1:8000/docs
uv run uvicorn mocks.main:app --port 8001  # mock payment/shipping/helpdesk, http://127.0.0.1:8001/docs
uv run python -m scripts.policy_search "my return hasn't arrived yet"  # see ranked policy citations
uv run python -m scripts.mint_token --reviewer "reviewer|ravi"  # token that can approve/reject
uv run python -m scripts.worker      # background worker: agent runs, approval resumes, expiry + refund reconcile sweeps
uv run python -m scripts.expire_proposals  # expire stale proposals now (the worker also does it every 5 min)
```

Without Docker (Homebrew Postgres 16): pgvector must be built from source for PG16, and since it
isn't a trusted extension, create it once as your superuser before migrating:
`psql -d support -c "CREATE EXTENSION IF NOT EXISTS vector"`.
