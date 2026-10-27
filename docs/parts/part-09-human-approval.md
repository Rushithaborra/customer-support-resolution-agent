# Part 9 — Human approval: reviewer roles, interrupt/resume, expiry, invalidation

## What was built

| File | Role |
| --- | --- |
| `app/auth.py` | New `reviewer` role and the `get_current_reviewer` dependency. Staff actors carry their role |
| `app/agent/checkpoint.py` | Durable LangGraph checkpoints (`PostgresSaver`) in their own `langgraph` schema |
| `app/agent/graph.py` | New nodes `await_approval` (uses `interrupt()`), `execute_refund` and `finalize`. Three writer nodes |
| `app/agent/resume.py` | `resume_for_proposal` and `resume_superseded`. Both are safe to repeat |
| `app/services/approvals.py` | `approve` / `reject` with four-eyes, `expire_stale` |
| `app/api/staff.py` | Reviewer-only approve/reject that resume the workflow. `POST /staff/maintenance/expire-proposals` |
| `migrations/versions/d4f6b8c0e2a3_…` | `action_proposals.created_by`, plus the `langgraph` schema and checkpoint tables |
| `scripts/expire_proposals.py`, `mint_token.py --reviewer` | Expiry job and reviewer tokens |
| `tests/test_approval_restart.py`, `tests/test_reviewer_roles.py` | 14 new tests (249 total) |

**Dependency:** `langgraph-checkpoint-postgres` 3.2.0.

## How it works

```
agent run ── … ── record (proposal created, run paused)
                      │
                await_approval ── interrupt() ──► ⏸ checkpoint written to Postgres (langgraph schema)
                      │                               process can stop here
   reviewer ──► POST /approve {payload_hash} ──► approvals.approve (role, four-eyes, hash)
                      │                         └► resume_for_proposal: Command(resume="approved")
                await_approval re-runs ── re-reads proposal status FROM THE DB
                      ├─ approved ─► execute_refund ─► refunds.execute (Part 4: FOR UPDATE, same key, exactly once)
                      │                    └────────► finalize ─► END
                      ├─ rejected / expired / invalidated ─────► finalize ─► END
                      └─ still pending (stray resume) ──► await_approval (pause again)
```

| Path | Proposal | Refund | Ticket | Run |
| --- | --- | --- | --- | --- |
| approve | executed | succeeded (one) | resolved | completed |
| reject | rejected | none | open | completed |
| expire (48h unreviewed, or approval unused after 24h) | expired | none | escalated | escalated |
| invalidated (a newer proposal superseded it) | superseded | none | owned by the newer proposal | completed |

**Roles:**
- `staff` operates: classify, run the agent, propose, execute manual refunds, close.
- `reviewer` decides: approve and reject.
- A reviewer can never decide a proposal they created (`created_by`), so `self_approval` returns 409.

**Verified across a real restart:** one process ran the agent until it paused, then exited. A freshly started API
approved over HTTP. The workflow resumed from Postgres, executed **one** ₹2,499 refund, and the ticket became `resolved`.

**Frontend (Part 12):** the approval queue's Approve and Reject buttons call these endpoints with a reviewer token.
The response's `run_status` and `refund.status` drive the outcome panel immediately. Expired items appear in the escalation view.

## Why each piece is necessary

| Piece | What breaks without it |
| --- | --- |
| Postgres checkpoints | A deploy or crash while a proposal waits (hours, maybe days) loses the workflow, and the approval leads nowhere |
| Separate `langgraph` schema | Checkpoint tables mix with the ledger and get mistaken for it, and Alembic tries to manage library-owned tables |
| Decision re-read from the DB | Anyone who can trigger a resume with `"approved"` could execute a refund. The forged-resume test checks this |
| "Pending" → pause again | A stray resume (bug, duplicate message) would end the workflow, and the real approval would have nothing to wake |
| Execution in its own node, via idempotent `refunds.execute` | `interrupt()` re-runs the node it's in. If the refund were in that node, every resume could pay again |
| Reviewer role separate from staff | Whoever runs the ticket can also authorise its money |
| Four-eyes on `created_by` | One person proposes and approves their own refund |
| Expiry job | Proposals wait forever, approvals go stale, and the reviewer approves based on week-old facts |
| Invalidation on supersede | Old paused threads pile up forever, and someone later "approves" a superseded proposal |
| Ticket → `awaiting_provider` on a provider 503 | The customer sees "awaiting approval" when approval already happened (found by a Part 9 test) |

## Tradeoffs and known limitations

- **Resume is synchronous.** The approve request waits for execution (one provider call). Part 10 moves it to a worker
  and returns `202 Accepted`.
- **Expiry runs on demand** (endpoint or script). Part 10's worker runs it on a schedule.
- **Agent proposals execute as soon as they're approved.** The tamper window is therefore *before* approval (caught as
  `payload_tampered`). Manual proposals keep the separate execute step, and the Part 4 tamper-after-approval tests still apply.
- **A provider outage during execution doesn't retry automatically.** The refund stays `submitted`, the ticket shows
  `awaiting_provider`, and staff refresh (or the Part 11 sweep) reconciles it.
- **Roles are single-valued** (one `role` claim). A real IdP would issue role lists or groups.
- **Checkpoints have no retention policy yet.** Finished threads keep their checkpoints until deleted.

## Interview questions

**1. Your workflow pauses for a human for two days and the server is redeployed. What happens?**
Nothing is lost. The pause is LangGraph `interrupt()` with a `PostgresSaver`, so the graph's position is a row in
Postgres, not process memory. When the reviewer approves, the new process builds the graph, finds the thread by the run's
`thread_id`, and resumes it with `Command(resume=…)`. I tested it both ways: in the suite, with a brand-new saver and
connections, and manually across two real processes, where the second one executed exactly one refund.

**2. What stops someone from resuming the graph with "approved" without an approval?**
The resume value is only a wake-up signal. `await_approval` re-reads the proposal's status from the database after it
wakes. If no reviewer approved it, the status is still `pending_approval`, so the graph routes back into `await_approval`
and pauses again. Even on the approved path, `refunds.execute` re-checks the stored approval: reviewer, expiry, and the
three-way payload-hash match. `test_forged_resume_without_approval_executes_nothing` covers it.

**3. LangGraph re-runs interrupted nodes on resume. How do you avoid paying twice?**
Two layers. First, the node that pauses (`await_approval`) is read-only, so re-running it does nothing. Second, the money
moves in a separate node, `execute_refund`, through `refunds.execute`, which is idempotent per proposal. It finds the
existing refund operation, reuses the same idempotency key, and the provider deduplicates. So a crash mid-execution
followed by a re-run continues the same refund. Checkpoints decide *where the workflow is*. The ledger tables decide
*what happened to money*, and only the ledger is trusted for that.
