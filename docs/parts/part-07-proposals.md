# Part 7 — Proposals: agent-drafted, code-validated, hash-bound

## What was built

| File | Role |
| --- | --- |
| `app/services/proposals.py` | `build_refund_payload` + `create_refund_proposal`: **one** builder for staff and agent |
| `app/domain/proposal_rules.py` | `validate_refund_draft`: pure checks, and every violation is reported |
| `app/agent/schemas.py` | `RefundProposalDraft` (order, amount, currency, evidence refs, rationale) |
| `app/agent/graph.py` | New read-only nodes `draft_proposal` and `validate_proposal`. `record` now creates the proposal |
| `app/services/resolution.py` | Staff `propose_refund` uses the shared builder. `list_proposals` added |
| `app/api/staff.py` | `GET /staff/proposals?status=pending_approval` (the reviewer queue) |
| `tests/test_proposal_rules.py`, `test_agent_proposals.py`, `test_proposals_api.py` | 27 new tests (197 total) |
| `tests/agent_scripts.py` | Shared scripted-LLM building blocks (moved out of the Part 6 test file) |

No migration: proposals already had `payload` (JSONB) and `payload_hash`.

## How it works

```
route = propose_refund (code)
   │
draft_proposal  (LLM)  →  RefundProposalDraft {order_id, amount_minor, currency, evidence_refs, rationale}
   │                       shape checked by repair-or-stop; failure → escalate(proposal_draft_failed:…)
validate_proposal (CODE)
   │   fresh facts → evaluate_refund → validate_refund_draft(draft, resolved order, decision, trace)
   ├── violations → escalate(proposal_rejected:<first code>), all violations stored, NO proposal
   └── ok → build_refund_payload(...)  ← amount from the rules, refs + rationale from the draft
record (only writer)
   └── create_refund_proposal: pending_approval + payload_hash, run paused, ticket awaiting_approval
then (unchanged, Part 4): staff approve {payload_hash} → execute → provider → verify
```

| Check (code) | Violation |
| --- | --- |
| Draft order is the order code resolved | `wrong_order` |
| Currency matches the order | `wrong_currency` |
| Amount **equals** the rules' refundable amount (v1: full refunds only, so over and under both fail) | `amount_mismatch` |
| Every evidence ref is a **successful** tool call **in this run** (`tool:order_id`) | `unsupported_evidence` |
| Code's eligibility decision is cited (`check_refund_eligibility:<order>`) | `missing_required_evidence` |
| Still eligible on fresh facts | `not_eligible` |

**The payload a reviewer approves** (inside the hash): `action, ticket_id, order_id, amount_minor (code), currency,
reason_code, policy_version, evidence{return…}, evidence_refs, proposed_by, rationale`.
Staff proposals carry code refs (`code:evaluate_refund:ord_1001`, `db:return:ret_1001`) and `proposed_by: staff:<sub>`.

**Frontend (Part 12):** the approval queue lists `GET /staff/proposals?status=pending_approval`. Each card shows the amount,
`proposed_by`, the rationale and the evidence refs, each linked to its row in the run's trace. The Approve button sends back
`payload_hash`. Rejected drafts appear on the escalation screen with their violations.

## Why each piece is necessary

| Piece | What breaks without it |
| --- | --- |
| Amount from the rules, draft amount only **compared** | A model that writes 2499000 instead of 249900 (an extra zero) creates a ₹24,990 proposal. A tired reviewer might approve it |
| Evidence refs must match **this run's successful** calls | The model cites "get_return_status" it never called, or a call that failed, and the reviewer trusts evidence that doesn't exist |
| Eligibility ref required | A proposal could rest only on the model's reading of free text, not on code's decision |
| Refs, rationale and `proposed_by` inside the hash | Someone swaps the evidence or the rationale after approval, and the approval still "matches" |
| Reject → escalate, not auto-correct | Silent correction hides the model's error. The reviewer sees a perfect proposal and assumes the agent is reliable |
| All violations collected | The reviewer fixes one problem, re-runs, and finds the next one. The eval also undercounts error types |
| One shared builder | The staff and agent paths drift apart, and one of them skips a check |
| Proposal created in `record` (single writer) | A re-run node on resume (Part 9) would create a duplicate proposal. `record` also reuses an existing proposal for its run |

## Tradeoffs and known limitations

- **One extra LLM call per refund** (the draft). Drafting is cheap compared to the cost of a wrong refund, and it gives the eval
  a direct measure of whether the model gets money right.
- **Exact-amount rule.** v1 has no partial refunds, so any difference is wrong. Partial returns would need per-item
  amounts in both the draft and the rules.
- **Evidence refs prove a call happened and succeeded, not that the model read it correctly.** The amount check and
  code's eligibility decision cover the part that matters for money. Reading quality is measured in Part 13.
- **The repair step only fixes shape.** It never shows the model the code's amount, so a retry can't just copy the right number.
- **No four-eyes rule yet:** `proposed_by=agent` means any staff member can approve. Part 9 adds reviewer roles.
- **The queue is a flat list** (newest first, filter by status). Paging and assignment come with the Part 12 UI.

## Interview questions

**1. The agent proposes ₹24,990 for a ₹2,499 order. What happens?**
`validate_proposal` re-reads the order under the same rules used for execution: refundable is 249900 paise, and the
draft says 2499000. That's `amount_mismatch`, so no proposal is created, the run escalates with
`proposal_rejected:amount_mismatch`, and the draft plus the violation are stored for review. Even if the check
were missing, the payload's amount is built by code from the rules, not copied from the draft, so the model's number can't
reach the payment provider either way. `test_wrong_amount_is_rejected_and_escalated` covers it.

**2. What does an evidence ref actually guarantee?**
That the proposal points at a tool call that really happened in this run, for this order, and succeeded. It's checked
against the run's own trace, not against what the model says it did. It doesn't guarantee the model interpreted the result
correctly. That's why the one fact money depends on (eligibility and amount) comes from code, and why the eligibility ref is
mandatory. Refs make the proposal reviewable: a reviewer can click each one and see the exact tool result.

**3. Why reject a bad draft instead of quietly fixing it with the correct amount?**
Because fixing hides information that matters. If the system silently corrects the amount, the reviewer sees a perfect
proposal and starts to trust the agent's drafts, and the eval never sees how often the model gets money wrong. Escalating
with exact violations keeps a human in charge of anything unusual and makes the model's error rate visible. The Part 4 staff
path is still there, so a human can create a correct proposal in one call.
