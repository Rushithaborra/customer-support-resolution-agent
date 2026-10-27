# Part 3 — Mock external services

## What was built

| File | Role |
| --- | --- |
| `mocks/main.py` | A separate FastAPI app for the three "other companies'" services. Run it with `uv run uvicorn mocks.main:app --port 8001` |
| `mocks/payment.py` | Refund create, get, and lookup by idempotency key. Status is `processing`, `succeeded` or `failed` |
| `mocks/shipping.py`, `mocks/data.py` | Read-only tracking that matches the seed orders (`ord_1003` in transit, `ord_1002`'s return in transit) |
| `mocks/helpdesk.py` | Customer-visible replies, posted idempotently |
| `mocks/idempotency.py` | The shared idempotency-key contract |
| `mocks/faults.py`, `mocks/admin.py` | Fault injection (500, 503, timeout before/after apply, slow), reset, and refund settlement |
| `mocks/config.py` | Provider API-key check |
| `app/integrations/results.py` | `Ok`, `Rejected`, `Retryable`, `UnknownOutcome` |
| `app/integrations/http.py` | One place that turns every HTTP outcome into one of those four results |
| `app/integrations/{payment,shipping,helpdesk}.py` | Typed clients that parse responses into Pydantic models |
| `tests/test_mock_idempotency.py`, `tests/test_integration_clients.py` | 23 new tests (49 total) |

No schema change, and no route in `app/` uses these clients yet.

## How it works

**The idempotency contract** (payment and helpdesk writes):

| Request | Response |
| --- | --- |
| New `Idempotency-Key` | Apply, store `(sha256 of canonical body, response)`, return **201** |
| Same key, same body | **200**, the *stored* response (same `refund_id`), `Idempotent-Replayed: true` |
| Same key, different body | **422** `idempotency_key_reused`; nothing applied |
| Same key while the first is still running | **409** `request_in_progress` |
| No key | **400** |
| A failed request (5xx before apply) | The key is released, so a retry with the same key can succeed |

**How a client call becomes a result** (`app/integrations/http.py`):

```
request ──► connect refused / connect timeout ─────────────► Retryable      (never sent)
        ──► read timeout / connection dropped ── write? ───► UnknownOutcome
                                               └ read? ────► Retryable
        ──► 2xx ───────────────────────────────────────────► Ok(model, replayed?)
        ──► 503, 429, 409 in_progress ─────────────────────► Retryable      (provider refused)
        ──► other 5xx ───────────────────────── write? ────► UnknownOutcome
        ──► 4xx ───────────────────────────────────────────► Rejected       (don't retry)
```

The clients **never retry on their own**. What to do with an `UnknownOutcome` is a decision
for the execution step (Part 11): look the refund up by idempotency key, and only then decide.

**The two timeouts that look identical:**
```
timeout_before_apply:  client ──req──► provider drops it        client sees: timeout
timeout_after_apply:   client ──req──► provider REFUNDS, stores  client sees: timeout
```
Same error, opposite reality. `find_refund_by_key` is the only way to tell them apart.
The tests check both cases.

**Trying it by hand:**
```bash
uv run uvicorn mocks.main:app --port 8001
curl -X POST localhost:8001/payment/refunds -H 'Authorization: Bearer dev-mock-key' \
  -H 'Idempotency-Key: demo-1' -H 'content-type: application/json' \
  -d '{"order_id":"ord_1001","amount_minor":249900,"currency":"INR"}'   # 201, then 200 on repeat
# Make the next refund time out *after* it is applied:
curl -X POST localhost:8001/_admin/faults -H 'Authorization: Bearer dev-mock-key' \
  -H 'content-type: application/json' -d '{"kind":"timeout_after_apply","operation":"payment.create_refund"}'
```

**Frontend (Part 12):** nothing user-facing yet. Later, the outcome panel shows `processing`
and `succeeded` as different states, because the provider reports them separately.

## Why each piece is necessary

| Piece | What breaks without it |
| --- | --- |
| Idempotency key on every write | A retry after a timeout refunds twice |
| Body hash stored with the key | A bug or a retry with a *different* amount gets the old success back and silently looks fine |
| `UnknownOutcome` as its own result | Code treats a timeout as failure, retries with a new key, and the customer is refunded twice |
| Reconciliation lookup by key | After a timeout there is no way to learn whether money moved |
| No auto-retry in the client | Retries happen in a place that can't check the ledger or the approval |
| Explicit connect/read timeouts | A hung provider holds a worker (and later a DB row lock) forever |
| 503 → retryable but generic 500 → unknown | Treating every 5xx as "nothing happened" is wrong; a 500 can arrive after the write |
| `StrictInt` on amounts (both sides) | `2499.0` gets coerced, and a float slips into the money path |
| Real uvicorn server in tests | In-process transports don't enforce timeouts, so the timeout tests would test nothing |
| Mocks outside `app/`, with their own config | The "provider" quietly shares our code or DB and hides integration bugs |

## Tradeoffs and known limitations

- **In-memory provider state.** It resets when the mock process restarts. That's fine because Part 11's restart
  tests restart *our* worker, not the provider. A persistent mock would be needed to simulate provider restarts.
- **One process for three services.** This is simpler to run. Separate ports would add nothing the tests need.
- **No provider-side over-refund check.** The mock accepts any positive amount. Keeping total refunds
  at or below the amount paid is enforced on our side in Part 11 (`SELECT ... FOR UPDATE`), and a real
  provider check would be a second layer, not the only one.
- **Replays return the original response**, so a `processing` refund replays as `processing` even after it
  settles. This matches how real providers behave. Use `get_refund` for the current status.
- **The idempotency store never expires keys.** Real providers expire them after about 24 hours.
- **Same API key for admin and data endpoints.** The admin endpoints are test-only.
- **No linkage yet** between our tickets and helpdesk ticket ids. That comes in the part that sends replies.

## Interview questions

**1. Your refund call times out. What do you do?**
Not retry blindly, and not mark it failed. A timeout on a write is an *unknown outcome*:
the provider may have processed it and the response got lost. I look the refund up by its
idempotency key. If it exists, I record the provider's result. If not, I can retry **with
the same key**, so even a race between my lookup and a late-arriving original can't create
a second refund, because the provider replays the first. The tests
`test_timeout_after_apply_is_unknown_and_refund_exists` and `..._before_apply_..._absent`
show the two cases look identical to the caller.

**2. Why store a hash of the request body with the idempotency key?**
The key alone says "same operation", but it doesn't prove the request is the same. If a bug reuses a key
for a different amount, a key-only check returns the old success and the new request silently
never happens, or looks like it did. Comparing the canonical-JSON SHA-256 makes reusing a key with a different
body a loud 422. It's the provider-side twin of Part 1's `payload_hash` on approvals.

**3. Why does a 503 count as retryable but a 500 as unknown?**
A 503 or 429 is the provider explicitly saying "I didn't take this, try later", so nothing was applied.
A generic 500 only says something went wrong, possibly after the database write. For reads
the difference doesn't matter, but for a refund, guessing "nothing happened" is how double refunds
happen, so the client reports `UnknownOutcome` and the caller reconciles first.
