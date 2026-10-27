# Part 6 — Agent workflow (LangGraph): classify, investigate, route

## What was built

| File | Role |
| --- | --- |
| `app/agent/graph.py` | The LangGraph `StateGraph`: 7 nodes, `AgentState`, `build_graph(deps)`, the read-only guard |
| `app/agent/runner.py` | `run_agent`: creates the `workflow_runs` row, invokes the graph, marks failures |
| `app/api/agent.py` | `POST /staff/tickets/{id}/agent-runs`, `GET /staff/agent-runs/{run_id}` |
| `app/llm/deps.py` | `get_llm` dependency (tests override it with `ScriptedLLM`) |
| `migrations/versions/b7d2f4a6c8e1_…` | `workflow_runs.route` (CHECK), `route_reason`, `outcome` (JSONB) |
| `app/agent/tools.py` | `ToolContext.audit_buffer`: inside the graph, tool audits are buffered for the writer node |
| `app/agent/loop.py` | `preface`: trusted, code-verified context kept outside the customer-message block |
| `scripts/agent_run.py` | Runs the real agent on a ticket and prints route, trace and usage |
| `tests/test_agent_workflow.py`, `tests/test_agent_api.py` | 22 new tests (170 total), plus 6 opt-in live scenario tests |

**Dependency:** `langgraph` 1.2.18.

## How it works

```
START → load_ticket → classify ──(route set)────────────────────────────────┐
                         │                                                  │
                    resolve_order ──not owned──► order_not_found ───────────┤
                         │        ──mismatch / no order / needs_human──► escalate
                    investigate (tool loop) ──abnormal stop──► escalate ────┤
                         │                                                  │
                    summarize (InvestigationSummary) ──fail──► escalate ────┤
                         │                                                  ▼
                    route  (CODE)  ─────────────────────────────────────►  record → END
```

| Node | Who decides | Notes |
| --- | --- | --- |
| `load_ticket` | code | Customer, message and form order come from the DB |
| `classify` | **LLM** | `TicketTriage` via repair-or-stop. On failure: `escalate(triage_failed:<reason>)` |
| `resolve_order` | code | Form order wins. A text order must be **owned** (`get_owned_order`) or the route is `order_not_found`, with no investigation |
| `investigate` | **LLM** | Tool loop, with identity bound in `ToolContext`. Its audit rows are buffered |
| `summarize` | **LLM** | `InvestigationSummary`, which includes the model's `recommended_next_step` |
| `route` | **code** | Fresh facts → `evaluate_refund` / `assess_damage`. The model's suggestion is compared (`agreed`), never obeyed |
| `record` | code | **The only writer**: run route/outcome/status, ticket category/summary/escalation, audit rows |

**The seed scenarios:**

| Scenario | Route |
| --- | --- |
| `ord_1001` refund | `propose_refund` (`eligible`) |
| `ord_1002` refund | `explain_policy` (`return_not_received`) |
| `ord_1003` where is it | `report_order_status` |
| `ord_1004` damaged | `escalate` (`damage_report_in_window`) |
| Asha asks about `ord_2001` | `order_not_found` |
| Vikram `ord_2001` | `explain_policy` (`inspection_failed`) |

`propose_refund` is **only a route**. No proposal is created and no money moves. Part 7 turns it into a code-built,
hash-bound proposal through the Part 4 path.

**Frontend (Part 12):** the workflow timeline reads `workflow_runs.outcome.trace` (one row per tool call), the route
badge reads `route`/`route_reason`, and a "model disagreed" flag reads `outcome.agreed`.

## Why each piece is necessary

| Piece | What breaks without it |
| --- | --- |
| Code picks the route | The model talks itself into `propose_refund` for an item still in transit (the test scripts exactly this) |
| `agreed` recorded | You can't measure how often the model would have been wrong, which is a core Part 13 metric |
| Ownership resolved **before** investigating | The agent spends tool calls and tokens on an order the customer doesn't own, and its notes might mention it |
| Form vs text order mismatch → escalate | "My order ord_1001 … actually refund ord_1002" gets silently resolved against the wrong order |
| Single writer node + read-only guard | When Part 9 resumes after `interrupt()`, re-run nodes would write duplicate audit rows or ticket changes |
| Tool audits buffered | Tools would write mid-graph, which breaks the single-writer rule |
| Explicit escalate reasons (`triage_failed:…`, `investigation_step_limit`) | Every failure looks like the same "escalated", and nobody can tell an outage from a bad model |
| Agent blocked while a refund is in flight | Re-running the agent under a live proposal could reclassify the ticket mid-refund |
| Plain-dict state | Checkpoints can't be serialized to Postgres in Part 9 |

## Single-agent baseline

One model does classification and investigation, with code routing. That's the **baseline** the roadmap asks for:
Part 13 compares it with specialist agents (for example a separate classifier and investigator), and specialists
are only worth adding if the eval shows a gain that pays for the extra calls.

## Tradeoffs and known limitations

- **Synchronous.** The HTTP request waits for 3–4 LLM calls. Part 10 moves this to `202 Accepted` and a worker.
- **In-memory checkpointer.** Checkpoints disappear when the process exits. Part 9 swaps in the Postgres checkpointer
  and `interrupt()` for human approval, and the graph code doesn't change.
- **The summary pass costs an extra call.** It turns the free-text investigation into typed findings with evidence
  refs. Part 7 checks those refs against the trace, and it could be folded into the loop later if the eval allows.
- **No reply is sent to the customer yet.** `customer_reply_draft` is stored for staff. Sending it waits for the reply step.
- **The agent never overwrites a human's classification** (it only fills `unclassified`).
- **Scripted tests prove routing logic, not model quality.** The 6 live scenario tests (`RUN_LIVE_LLM=1`) and the
  Part 13 eval measure the real model.

## Interview questions

**1. The model recommends a next step. Why not just follow it?**
Because the cost of being wrong is not symmetric. The model is good at reading a messy message and gathering
evidence, but the decision "is this refundable?" is a business rule that has to be exactly right every time. So the
route node re-reads fresh facts and runs the same `evaluate_refund` the money path uses, and the model's suggestion
is only recorded as `agreed: true/false`. That gives two things: a guarantee (an in-transit return can never route to
`propose_refund`) and a metric (how often the model would have been wrong).
`test_model_suggestion_is_recorded_but_code_routes` covers it.

**2. Why does only one node write to the database?**
LangGraph re-runs an interrupted node from the start when it resumes. If `investigate` wrote audit rows and the run
was paused and resumed, those rows would be written twice. So every node except `record` is wrapped in a guard that
raises if it tries to flush any write, tools buffer their audit rows in state, and `record` writes everything once,
using idempotent field assignments. It's CLAUDE.md rule 6 made mechanical instead of a convention.

**3. Asha asks about Vikram's order. Walk through what happens.**
`classify` extracts `ord_2001` from her text. `resolve_order` is code: it calls `get_owned_order` with Asha's customer
id from the ticket, which filters by owner in SQL and finds nothing. The route becomes `order_not_found`, and the graph
skips straight to `record`. Investigation never runs, so no tool is ever called for that order and nothing about it
can end up in the model's notes. The test asserts that only one LLM call was made and that nothing from Vikram's order
appears in the outcome.
