# Part 10: Async runs (Postgres job queue, worker, 202 Accepted, run timeline)

## What was built

| File | Role |
| --- | --- |
| `app/db/models.py`, `migrations/versions/e5a7c9d1f3b5_…` | New `jobs` table (lease, attempts, backoff, `dedupe_key`). Runs gain the `queued` status |
| `app/jobs/queue.py` | `enqueue` (outbox), `claim` (`FOR UPDATE SKIP LOCKED` plus lease takeover), `heartbeat`, `succeed`, `fail`, `bury` |
| `app/jobs/worker.py` | Handlers (`agent_run`, `resume`, `expire_sweep`), error classes, `drain`, the `serve` loop and the sweep schedule |
| `app/agent/runner.py` | Split into `start_run` (request side) and `execute_run` (worker side). `run_agent` stays as an inline helper |
| `app/agent/resume.py` | `request_resume` / `request_resume_superseded` queue the resume. The worker runs `resume_for_proposal` |
| `app/api/agent.py` | `POST …/agent-runs` returns **202** plus `Location`. The run shows its latest job. New `GET /staff/agent-runs/{id}/events` |
| `app/api/staff.py` | Approve and reject store the decision, then queue the resume: **202** for agent proposals, 200 for manual ones. Expiry queues resumes |
| `scripts/worker.py` | `uv run python -m scripts.worker` (`--once` drains the queue and exits) |
| `tests/test_worker_restart.py`, `test_jobs_queue.py`, `test_agent_runs_async.py` | 16 new tests (275 total) |

No new dependencies. The queue is one Postgres table.

## How it works

```
staff ──► POST /staff/tickets/{id}/agent-runs
            start_run: lock ticket, check, INSERT run (queued) + audit + job ── one COMMIT ──► 202 + Location
                                                                                  │
worker ──► claim: SELECT … FOR UPDATE SKIP LOCKED  (due, or lease expired) ───────┘
            status=running, attempts+1, locked_until = now + 10 min ── COMMIT (lock released)
            execute_run:
              checkpoint has pending steps? ── yes ─► graph.stream(None)       (carry on where the dead worker stopped)
                                             └─ no ─► graph.stream({run_id, ticket_id})
              after every node: audit "node.completed" + heartbeat (extend lease, still ours?) ── COMMIT
            succeed │ fail → retry with backoff (5s·2^n) │ bury after 3 → run failed, ticket escalated

reviewer ──► POST /approve ─► store approval ─► enqueue resume job ── COMMIT ──► 202 {resume_job_id}
worker   ──► resume job ─► resume_for_proposal ─► execute_refund (Part 4 exactly-once) ─► finalize
worker   ──► every 5 min: one expire_sweep per time bucket (dedupe key) ─► expire_stale ─► resume jobs
```

**Timeline (`/events`).** Every run's `audit_events` in order: `agent.queued`, `job.claimed` (worker, attempt,
`taken_over`), one `node.completed` per graph step, `tool.called`, `agent.routed`, `agent.paused`, `proposal.approved`,
`refund.*`, `agent.resumed`, `job.dead_lettered`. Payloads hold ids, node names and codes, never customer text.

**What a crash looks like (verified with real processes):**
1. Worker A finished `load_ticket`, `classify` and `resolve_order`, then was `kill -9`'d during `investigate`.
2. After the lease ran out, worker B claimed the job (`attempt 2, taken_over: true`). It re-ran only `investigate`,
   with no second triage call, and paused for approval.
3. Approve returned 202. The worker executed one refund and the ticket resolved.

## Why each piece is necessary

- **A job table instead of `BackgroundTasks`.** `BackgroundTasks` live in the API process's memory, so a restart
  loses the work. A row in Postgres survives any process. The queue is also visible: you can query what's stuck and why.
- **Outbox: the job commits with the run.** If they committed separately, a crash in between would leave a run nobody
  will ever pick up, or a job pointing at nothing. One transaction makes them appear together or not at all.
- **`FOR UPDATE SKIP LOCKED`.** Several workers can claim at once. `FOR UPDATE` stops two of them taking the same
  row. `SKIP LOCKED` makes the second one move on to the next row instead of waiting.
- **The claim commits immediately.** The row lock lasts milliseconds, not the length of the job. Holding a
  transaction across LLM and provider calls would break rule 5 and pin a connection for minutes.
- **The lease (`locked_until`).** A killed worker can't say "I died". The lease running out says it for them, and
  then another worker may take over. Without it, a crashed job stays `running` forever.
- **The heartbeat after each node.** A slow step (a local LLM can take a minute) mustn't look like a death. The
  heartbeat only extends the lease *if it is still ours*. If someone else took over, the old worker gets `LeaseLost`
  and stops without writing anything.
- **Bury after `max_attempts`, even for crashes.** A job that kills its worker every time (a poison pill) would
  otherwise loop forever. A dead agent run escalates its ticket to a human.
- **Retryable vs permanent errors.** A bad API key or a read-only violation fails the same way every time, so it
  dead-letters at once. Anything else gets backoff retries.
- **`durability="sync"`.** Each step's checkpoint is written before the next step starts, so a crash loses at most
  the step it was in.
- **Resume from the checkpoint is safe (rule 6).** The interrupted node re-runs, but non-writer nodes are read-only.
  `execute_refund` is idempotent per proposal: same operation, same idempotency key, and it reconciles a `submitted`
  refund before ever resending. `test_worker_killed_inside_execute_refund_refunds_once` kills the worker *after* the
  provider accepted the refund. The next worker finds it by key, and the provider still has exactly one refund.
- **Approve and reject queue the resume.** The provider call no longer happens while the reviewer's request is open.
  It returns 202 because the refund isn't done yet. A manual proposal has nothing to resume, so it stays 200.
- **One active run per ticket (`409 run_in_progress`).** The ticket row is locked while checking, so two clicks
  can't start two agents on the same ticket.
- **Dedupe keys.** A double approve, or two workers scheduling the same sweep, collapse to one job.

## Tradeoffs and known limitations

- **Polling** (1 s when idle), not `LISTEN/NOTIFY`. Simple and robust, with a little latency and some idle queries.
- **Heartbeat per node, not per second.** A single step longer than the 10-minute lease could be taken over while
  still running. The `LeaseLost` check stops the old worker from writing, and every step is safe to re-run, so the
  cost is wasted work, not a wrong result.
- **No priorities or per-kind queues.** Jobs run oldest first.
- **The queue shares the business database.** That's fine at this scale and it's what makes the outbox possible. A
  high-volume system might move to a dedicated broker and keep an outbox table to feed it.
- **An LLM outage still escalates the run** (Part 6 behaviour: the graph turns `LLMUnavailable` into
  `triage_failed:llm_unavailable`) rather than retrying the job. Switching that to a retry is a small follow-up.
- **A provider outage during execution** still leaves the refund `submitted` and the ticket `awaiting_provider`.
  Part 11's reconciliation sweep picks those up.
- **`run_agent` still runs inline** (scripts and older tests). The API never uses it.
- **No retention policy** for finished jobs, audit events or checkpoints yet.

## Interview questions

**1. Why build a queue on Postgres instead of using Celery or Redis?**
The job has to be created atomically with the business row: the run and its job commit in one transaction (outbox).
With a separate broker I'd need the outbox table *plus* a relay process to publish from it. With
`SELECT … FOR UPDATE SKIP LOCKED`, Postgres already gives safe concurrent claiming, and the queue is queryable with
SQL. The cost is polling and sharing the DB's capacity, which is fine at a few jobs per second. If load grew, I'd keep
the outbox and add a broker behind it.

**2. A worker is `kill -9`'d halfway through an agent run. Walk me through what happens.**
Its job row is still `running` with `locked_until` in the future, so no one touches it while the lease is live. Once
the lease expires, the next claim's query matches it, and another worker takes it over (attempt 2, recorded in the
timeline). That worker finds the LangGraph checkpoint has pending steps, so it calls `stream(None)` and continues
after the last completed node. Finished LLM steps aren't repeated. The interrupted node re-runs, which is safe because
non-writer nodes can't write, and money only moves in the idempotent `execute_refund`. If the job kills its worker
three times, it's buried and the ticket escalates.

**3. How do you know a takeover never causes a double refund?**
Three things. The refund step is idempotent per proposal: one operation row, a stable idempotency key, and an existing
`submitted` refund is reconciled with the provider before any resend. The worker that lost its lease can't keep
writing, because its heartbeat fails with `LeaseLost`. And it's tested at the worst moment: the provider accepted the
refund and the worker died before recording it. The next worker found it by key, the provider shows exactly one
refund, and the ticket resolved.
