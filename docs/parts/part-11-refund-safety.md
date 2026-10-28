# Part 11: Refund safety (automatic reconciliation and a zero-duplicate proof)

## What was built

| File | Role |
| --- | --- |
| `migrations/versions/f6b8d0e2a4c6_…`, `app/db/models.py` | `refund_operations.reconcile_attempts / next_reconcile_at / needs_review_reason`, and the `reconcile_sweep` job kind |
| `app/services/refunds.py` | `reconcile_due` (the sweep), `_park` (hand to a human), `_cancel_unsent`, `list_unsettled`. `_reserve` and `_send` schedule the op, and `_record` clears the schedule once final |
| `app/jobs/worker.py` | `reconcile_sweep` handler. `PERIODIC` schedules it every 60 s, and the expiry sweep every 5 min |
| `app/api/staff.py`, `app/schemas.py` | `GET /staff/refunds?state=unsettled\|needs_review`. Refund responses show the reconcile fields |
| `app/services/tickets.py` | Fix: the customer outcome is built as `OutcomeOut`. This removed the suite's 6 pydantic serializer warnings |
| `tests/test_refund_safety.py` | 16 new tests (291 total): an 8-case failure matrix, limbo edges, and 3 real-concurrency races |

Part 4 had already built the exactly-once core:
- A stable key `refund-<proposal_id>` (UNIQUE).
- A `FOR UPDATE` reservation, committed before the call.
- `UnknownOutcome` recorded as `pending_reconciliation`, never `failed`.
- `refresh()`, which asks the provider before ever resending.

Part 11 makes sure that core runs **without a person clicking refresh**, and proves the zero-duplicate claim at every
failure point.

## How it works

```
 reserved ──(commit; next_reconcile_at = now+60s)──► submitted ──(provider call, no txn open)──┐
    │                                                                                         │
    │ crash before send: the sweep finds it                    ┌──────────────────────────────┤
    ▼                                                          ▼                              ▼
 approval still valid → send          pending_reconciliation (timeout / 500)       processing (accepted, settles later)
 approval expired → failed (never sent: releases the balance; ticket escalated)
                                                               │                              │
               reconcile sweep, every 60 s, for ops whose next_reconcile_at is due ◄──────────┘
                 pick: FOR UPDATE SKIP LOCKED, attempts+1, book the next slot (30s·2^n, max 1h) ── COMMIT
                 then, no transaction open: refresh() = ask by key/reference
                   ├─ provider has it ─────────► record its status (succeeded / failed / processing)
                   ├─ provider has none ───────► resend with the SAME key (provider dedupes a late original)
                   ├─ provider can't answer ───► change nothing; the booked slot asks again
                   ├─ provider disagrees ──────► park: needs_review = provider_mismatch, ticket escalated
                   └─ 8 attempts used up ──────► park: needs_review = reconcile_exhausted (status unchanged)
```

When an op is parked, the sweep leaves it alone. Staff see it in `GET /staff/refunds?state=needs_review`. A staff
`refresh` that reaches a final state clears the flag.

**Verified with real processes:**
1. The mock lost the provider's response (`timeout_after_apply`), so the op became `pending_reconciliation`. The
   customer saw "We're confirming its status", and the provider already had the refund.
2. The 12:16:07 sweep skipped it, because it was still inside the grace window.
3. The 12:17:07 sweep found it by key and marked it `succeeded`. The ticket was resolved, the customer saw
   "completed", and the provider had exactly one refund. Nobody refreshed by hand.

## Why each piece is necessary

- **The sweep.** Without it, every unknown outcome waits for a human who may never come. The ticket sits in
  `awaiting_provider`, and the customer is told "submitted" forever.
- **Scheduling in the same commit as the state change.** `_reserve` and `_send` set `next_reconcile_at` in the
  commit that creates or submits the op. So a crash at *any* later point leaves a row the sweep will find. There is no
  separate "remember to check this" step that could be lost.
- **The 60 s grace window.** Right after `submitted`, the original sender may still be mid-call. Asking then would
  find nothing and resend. That resend is *safe* (same key), but it's noise. The grace window keeps the sweep out of
  the sender's way. `test_sweep_racing_a_slow_send_still_refunds_once` breaks the rule on purpose and still gets one
  refund, because the key is the real guarantee.
- **SKIP LOCKED on the refund rows, and booking the next slot before the call.** Parallel sweeps split the work
  instead of asking about the same op twice. The commit releases the locks before any network call (rule 5).
- **"Unknown is never failed."** Marking an unknown outcome `failed` would release the balance, which allows a second
  refund while the first may have landed. Parked ops keep their unknown status and keep holding the balance.
- **Parking instead of retrying forever.** If the provider never answers, or answers with a record that disagrees with
  ours, a human has to look. Retrying forever would hide it. Auto-resolving a mismatch would trust the provider over
  our own ledger.
- **Cancelling a never-sent op whose approval expired.** The status flips to `submitted` before any call, so
  `reserved` provably never reached the provider. Marking it `failed` is accurate, and it releases the held balance.
  Otherwise the order could never be refunded again.
- **Real concurrency tests.** The race tests use separate connections and threads, not the rolled-back test session,
  so `FOR UPDATE` and SKIP LOCKED actually contend:
  - Three parallel executes of one proposal give one refund.
  - Two approved proposals on two tickets for the same order: one succeeds, and the other is refused by the
    eligibility check (`already_refunded`), because the first one's hold was visible under the lock.

## The failure-point matrix (`test_every_failure_point_ends_in_exactly_one_refund`)

| Fault during execution | State right after | After the sweep |
| --- | --- | --- |
| Provider 503 | `submitted`, 0 at provider | resent with same key → `succeeded`, 1 |
| Timeout before apply | `pending_reconciliation`, 0 | resent → `succeeded`, 1 |
| Timeout after apply | `pending_reconciliation`, 1 | found by key → `succeeded`, 1 |
| 500 on write | `pending_reconciliation` | `succeeded`, 1 |
| Slower than client timeout | `pending_reconciliation` | `succeeded`, 1 |
| Worker killed before the call | `submitted`, 0 | resent → `succeeded`, 1 |
| Worker killed after provider accepted | `submitted`, 1 | found by key → `succeeded`, 1 |
| Provider says `processing`, settles later | `processing` | read by reference → `succeeded`, 1 |

Every case also asserts:
- The provider never holds more than one refund, at any step.
- There is exactly one `refund_operations` row.
- The status is never `failed` while unknown.
- The customer is never told "completed" before the provider confirmed it.
- The ticket ends `resolved` and the run `completed`.

## Tradeoffs and known limitations

- **Polling the provider instead of webhooks.** Real providers push status changes. Polling is simpler and keeps
  working when a webhook is lost. A production system would do both, with webhooks for speed and the sweep as the
  safety net.
- **Fixed backoff and a fixed 8-attempt budget** (about 3 h). It isn't tuned per failure type.
- **Parked ops stay parked** until a human refreshes or contacts the provider. There's no reminder or SLA alerting yet.
- **The grace window is a constant (60 s)**, not derived from the client timeout settings.
- **`processing` refunds count against the attempt budget too.** A provider that takes more than about 3 h to settle
  gets parked. That's arguably right (a human should know), but it's a choice.
- **A single provider and currency.** Multi-provider routing would need the key and lookup per provider.

## Interview questions

**1. The payment API times out. Did the customer get refunded?**
We don't know, and the code says so: the op is `pending_reconciliation`, never `failed`, and it keeps holding the
order's balance. The customer is told we're confirming with the provider. Within a minute or so, the reconcile sweep
asks the provider using the idempotency key we sent. If the refund exists, we record its real status. If it doesn't,
we resend with the *same* key, so even a delayed original can't become a second refund. If the provider keeps not
answering, the op is parked for a human after about 3 hours, still marked unknown, not failed.

**2. Why not just retry the refund call on timeout?**
A timeout doesn't tell you whether the write was applied. A blind retry with a *new* key could pay twice. Our retry is
safe for two reasons: the key is stable per proposal, so the provider dedupes it, and we *ask first*. We only resend
when the provider says it has nothing under that key. The matrix proves it, including the nasty case where the
provider accepted the refund and our worker died before recording the answer.

**3. Two reviewers approve two different proposals for the same order at the same moment. What stops a double refund?**
Execution starts with a short transaction that takes `SELECT … FOR UPDATE` on the order row, re-runs the eligibility
rules on fresh facts, and inserts a `reserved` refund operation that holds the amount, then commits. The second
transaction waits on the lock, then sees the first one's hold, so eligibility refuses it (`already_refunded`). I
tested it with real threads on separate connections: exactly one provider refund.
