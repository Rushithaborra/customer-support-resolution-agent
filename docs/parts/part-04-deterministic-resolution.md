# Part 4 — Deterministic resolution (no LLM)

## What was built

| File | Role |
| --- | --- |
| `app/domain/policy.py` | Policy numbers in code: version, 30-day return window, 7-day damage window, 24 h approval TTL |
| `app/domain/eligibility.py` | `evaluate_refund`, `assess_damage`: **pure functions** over a facts snapshot |
| `app/domain/hashing.py` | Canonical-JSON SHA-256 for proposal payloads |
| `app/domain/customer_messages.py` | Refund/ticket state → the only text a customer sees |
| `app/services/facts.py` | Loads the facts snapshot. `lock=True` takes `SELECT ... FOR UPDATE` on the order |
| `app/services/resolution.py` | Classify, assess, propose, approve, reject, close |
| `app/services/refunds.py` | Execute and refresh/reconcile, following CLAUDE.md rule 5 |
| `app/api/staff.py` | `/staff/*` routes |
| `app/auth.py` | `get_current_staff` (`role: "staff"` claim), shared `_decode` |
| `app/integrations/deps.py` | Payment/shipping clients as FastAPI dependencies |
| `migrations/versions/a4c1e2f3b5d6_…` | Adds refund status `processing` and ticket status `awaiting_provider` |
| `scripts/mint_token.py --staff` | Staff dev tokens |
| `tests/test_eligibility.py`, `test_staff_auth.py`, `test_manual_resolution.py`, `test_migrations.py` | 47 new tests (96 total) |

## How it works

### The full path for `ord_1001`
```
customer  POST /tickets {message, order_id}                       ticket: open
staff     PATCH /staff/tickets/{id} {category: refund_request}
staff     GET  /staff/tickets/{id}/assessment    → eligible, refundable 249900, evidence
staff     POST /staff/tickets/{id}/proposals     → code builds payload + payload_hash
                                                   ticket: awaiting_approval
customer  GET  /tickets/{id}  → "Your refund request is awaiting approval."
staff     POST /staff/proposals/{id}/approve {payload_hash}   → Approval (reviewer_id from token, expires 24h)
staff     POST /staff/proposals/{id}/execute     → refund op: succeeded
                                                   ticket: resolved
customer  GET  /tickets/{id}  → "Your refund of ₹2,499.00 has been completed by our payment provider."
```

### Eligibility (pure code)
The rules run in a fixed order, and the first one that fails decides the reason code:
`not_delivered` → `no_return` → `return_rejected` → `return_not_received` → `inspection_pending` →
`inspection_failed` → `outside_return_window` → `already_refunded` → `eligible`.
Refundable = paid − money "held" by refund operations in `reserved`, `submitted`, `processing`,
`succeeded` or `pending_reconciliation`. Only `failed` releases it.

### Execution (rule 5)
```
validate approval          approved? not expired? approved_hash == proposal.hash == sha256(payload)?
   │
reserve  ── txn ─────────  SELECT order FOR UPDATE → rerun rules on fresh facts → amount ≤ refundable
   │                       INSERT refund_operation(reserved, key="refund-<proposal_id>")   COMMIT
mark submitted ── txn ───  COMMIT
   │
provider call ──────────── NO transaction open (a test asserts this)
   │
record ── txn ───────────  Ok → succeeded / processing        Rejected → failed (releases balance)
                           UnknownOutcome → pending_reconciliation (never "failed")
                           Retryable → unchanged, API 503
                           Provider amount/order ≠ ours → pending_reconciliation (verify, don't trust)
                           COMMIT; ticket/run/proposal follow the refund's state
```
- **Execute is idempotent.** It finds the proposal's existing operation and continues from its state.
- **Refresh** asks the provider (by key, or by reference for `processing`). If the provider has no record,
  it resends **with the same key**.

### What the frontend will show (Part 12)
The reviewer screen renders the assessment's `evidence` and the proposal's payload and hash, and its
Approve button sends that hash back. The customer panel renders only `outcome.message`.

## Why each piece is necessary

| Piece | What breaks without it |
| --- | --- |
| Rules as pure functions over a snapshot | Rules get tangled with SQL and can't be tested at every boundary (day 30 vs 31) |
| Code computes the amount; staff can't type it | A typo or a social-engineered reviewer refunds ₹24,990 instead of ₹2,499 |
| Rules re-run under `FOR UPDATE` at reserve time | Assess at 10:00, a second refund at 10:01, execute at 10:02 on stale facts: a double refund |
| Reviewer sends back the hash they saw | The proposal changes between review and click, and the approval covers something nobody read |
| Three-way hash check at execute | Someone edits the stored JSON after approval (the test does exactly this) |
| Idempotency key derived from the proposal | A retry, a crash or a double-click generates a new key, and the provider refunds twice |
| `reserved` vs `submitted` | After a crash you can't tell "never sent" (safe to send) from "maybe sent" (must reconcile) |
| `pending_reconciliation`, never `failed`, on timeout | Marking it failed releases the balance, and the next proposal refunds again |
| No transaction during the provider call | A slow provider holds the order's row lock and every other refund on that order stalls |
| Customer text from stored state only | The customer is told "completed" while the provider still says `processing` |
| `CAST(SUM(...) AS BIGINT)` | **Found by the tests:** Postgres `SUM(bigint)` returns NUMERIC, so a `Decimal` entered the money path |

## Tradeoffs and known limitations

- **No four-eyes rule.** The same staff member can propose and approve. Enforcing a different reviewer needs a
  `created_by` column on proposals, which is planned for Part 9 with full reviewer roles.
- **No background jobs.** Expiry is checked only when someone tries to execute, and reconciliation runs only
  when someone calls refresh. Part 9 adds expiry and Part 11 adds a reconciliation sweep.
- **No concurrency tests yet.** `FOR UPDATE` and the UNIQUE key are in place, but tests that race two executes on
  separate connections are Part 11's job.
- **A `Retryable` result keeps the operation `submitted`**, not `reserved` as first planned. A 409 `request_in_progress`
  is also `Retryable` and means a request *is* in flight, so "never sent" would be false. Being
  conservative here costs one extra reconciliation lookup.
- **Full-amount refunds only** (v1 scope: no partial returns).
- **Customer messages are fixed templates.** Part 5+ lets the LLM write replies, but the state they describe still
  comes from here.
- **Assessment is N+1 per ticket list** (the outcome is computed per ticket). That's fine at v1 volume.

## Interview questions

**1. You check eligibility when the proposal is created. Why check again at execution?**
Facts can change between proposal and execution: another refund lands, a return is
re-inspected. Eligibility at proposal time is advice for the reviewer. The check that guards money has to
run on facts read **under the row lock that also protects the insert**, otherwise two executions can
both read "₹2,499 refundable" and both proceed. So `_reserve` locks the order with `SELECT ... FOR UPDATE`,
re-runs `evaluate_refund`, and only then inserts the reservation, all in one short transaction.

**2. The payment call times out. What state do you record, and why not "failed"?**
`pending_reconciliation`. A timeout means I don't know: the provider may have refunded and the response got
lost. "Failed" would release the held balance, and then a new proposal could refund again: a double refund
caused by bookkeeping. Refresh then asks the provider by idempotency key. If it's found, I record
what the provider says; if not, I resend with the same key, which the provider deduplicates.
`test_timeout_after_apply_reconciles_to_one_refund` and `..._before_apply_resends_with_same_key` cover both cases.

**3. How does an approval stay bound to exactly what was approved?**
Three hashes must agree at execution time: the hash the reviewer was shown and sent back (stored as
`approved_payload_hash`), the hash stored on the proposal, and a fresh SHA-256 of the payload as it is stored
right now. The first catches "the proposal changed while I was reviewing it". The third catches "someone edited the
JSON after approval", which the test simulates by rewriting `amount_minor` in the database. Any mismatch is a 409,
and nothing is sent to the provider.
