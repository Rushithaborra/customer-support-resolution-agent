# Résumé entry and pitch

Every number below comes from this repo's test suite or `evals/reports/report.md`. They're measured on synthetic
data, so describe them that way: they show what the design guarantees, not business impact.

## Résumé bullets

**Customer Support Resolution Agent**: Python, FastAPI, LangGraph, PostgreSQL/pgvector, Anthropic/Ollama
· [github.com/Rushithaborra/customer-support-resolution-agent](https://github.com/Rushithaborra/customer-support-resolution-agent)

- Built an LLM agent that resolves refund, delivery and damage complaints end to end. The model investigates with
  read-only tools and cites versioned policy (pgvector RAG), while deterministic code owns eligibility, amounts and
  routing. The result: **0 unsafe refund proposals across 120 evaluation runs**, including prompt-injection cases.
- Engineered **exactly-once refund execution**: integer-paise ledger, `SELECT … FOR UPDATE` reservations, stable
  idempotency keys, and automatic reconciliation of unknown provider outcomes. Proven by tests that inject a fault
  at 8 failure points (timeouts, lost responses, killed workers, parallel executes) and each end in exactly one
  refund.
- Made human approval and long-running work **crash-safe**: hash-bound four-eyes approvals using LangGraph
  `interrupt()` with Postgres checkpoints, plus a Postgres job queue (`SKIP LOCKED`, leases, retries). A run whose
  worker is killed is taken over and resumes without repeating finished LLM steps.
- Designed a **60-case evaluation with a held-out split**, deterministic grading and an oracle label check. It showed
  per-category specialists beat a single agent on a local 7B model (**70% → 85% held-out routing accuracy, 18%
  fewer tokens**). Shipped with 460+ automated tests and a browser UI for the demos.

Shorter version (2 bullets):
- Built an LLM support agent (FastAPI, LangGraph, pgvector) where the model proposes and code disposes:
  **0 unsafe refund proposals in 120 eval runs**; refunds are human-approved and **exactly-once** under injected
  timeouts, crashes and races.
- Built a 60-case held-out evaluation with deterministic grading. Per-category specialists improved held-out routing
  from **70% to 85%** with 18% fewer tokens on a local 7B model.

## Interview one-liner

> "I built an AI support agent that resolves refund and order complaints end to end. The LLM investigates and
> proposes; deterministic code enforces authorization, eligibility and exactly-once refunds; a human approves every
> money movement; and I measured it on held-out cases. Zero unsafe proposals in 120 runs, including injection
> attempts and crash and retry scenarios."

## 60-second pitch

"Support teams answer 'where's my refund?' by checking four or five systems by hand. A plain chatbot can't, because
it can't see the facts and can't be trusted to act. I built an agent where the LLM does the fuzzy part (reading the
complaint, calling read-only tools, citing the right policy version) and ordinary code does everything that must be
exactly right: who the customer is, whether they own the order, eligibility, the amount in integer paise, and
whether a refund already happened.

Money only moves after a reviewer approves the exact payload by its hash, and the execution step is idempotent. I
prove it by killing the worker right after the payment provider accepts a refund: the next worker reconciles by
idempotency key and there's still exactly one refund. I evaluated it on 60 synthetic cases with a held-out split.
A local 7B model routes 70% correctly as a single agent and 85% with per-category specialists, and in every miss
the system escalated to a human instead of doing something wrong. Zero unsafe proposals across 120 runs."

## Likely follow-ups (answers are in the part explainers)

| Question | Where |
| --- | --- |
| How do you stop the model from refunding the wrong amount? | [Part 7](parts/part-07-proposals.md), Q1–Q3 |
| What happens if the payment API times out? | [Part 11](parts/part-11-refund-safety.md), Q1–Q2 |
| Two reviewers approve at the same time? | [Part 11](parts/part-11-refund-safety.md), Q3 |
| The server restarts while a refund awaits approval? | [Part 9](parts/part-09-human-approval.md), Q1 |
| A worker dies mid-run? | [Part 10](parts/part-10-async-runs.md), Q2–Q3 |
| Why a Postgres queue, not Celery? | [Part 10](parts/part-10-async-runs.md), Q1 |
| How do you know your eval labels are right? Why no LLM judge? | [Part 13](parts/part-13-evaluation.md), Q1–Q2 |
| Were specialists worth it? | [Part 13](parts/part-13-evaluation.md), Q3 |
| Prompt injection via the customer message or a policy document? | [Part 8](parts/part-08-policy-rag.md), [Part 12](parts/part-12-frontend.md), Q1 |

## Framing to keep honest

- It's a **portfolio project on synthetic data with mock providers**, not a production deployment. Don't claim users,
  revenue or saved hours.
- The eval is **small** (20 held-out cases, so one case is 5 points) and **one run per mode**. Say "on this suite".
- The demo UI uses a **deterministic demo model** (labelled "not an AI"). Real-model numbers come from `qwen2.5:7b`.
