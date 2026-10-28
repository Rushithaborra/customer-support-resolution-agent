# Dev Log

## 2026-10-28

Today the project went from "works when everything goes right" to finished, measured and presentable: the free local model, then Parts 10 to 14 of the roadmap, 31 commits. All 14 roadmap parts are done, and 479 tests pass.

**Local model.** The `qwen2.5:7b` download had stalled; it was restarted and finished. On the 9 live tests the local model passed 7. The two misses were escalations to a human, never a wrong action. Each agent run takes about a minute on the laptop.

**Parts 10 and 11: surviving failure.** Agent runs moved out of the web request. Starting a run or approving a refund now saves a job and returns `202 Accepted` straight away, and a separate worker process does the work (Part 10):
- Jobs live in a Postgres queue that workers claim with `SELECT … FOR UPDATE SKIP LOCKED`, so two workers never take the same job.
- Each job has a time-limited lease. If a worker dies, another one takes over and continues from the run's last saved step.
- Failed jobs retry with backoff and, after 3 attempts, go to a human.

Refunds whose outcome is unknown (a timeout, a lost response, a crash) are now settled automatically: a sweep asks the payment provider using the refund's idempotency key. If the provider never answers, the refund is parked for a human; it is never marked "failed" (Part 11). Tests inject a fault at 8 different points, and every one ends with exactly one refund. Both parts were also checked live with real processes, including killing a worker in the middle of a run.

**Part 12: a UI.** A plain HTML + JavaScript UI is served by the API:
- A customer panel where the customer submits a request and watches its status.
- A staff/reviewer console with a live timeline of every agent step, an evidence panel, an approval queue, and buttons that make the payment provider fail on purpose.
- A deterministic "demo" model (keyword rules, labelled "not an AI") makes Demos A, B and C repeat exactly.

Building it exposed two real bugs, both fixed: picking an order from the dropdown used to escalate the ticket, and the HTTP client crashed on empty replies.

**Part 13: measurement.** An eval suite of 60 synthetic cases (40 dev, 20 held-out) runs through the real agent and is scored from stored results, with no AI judge. A "perfect model" check confirms every label. On the local model, the single agent routed 70% of held-out cases correctly, and a new specialist mode (separate investigators for refunds, delivery and damage) routed 85% with 18% fewer tokens. There were **zero unsafe refund proposals and zero data leaks in all 120 runs**, prompt-injection cases included. The most common miss was the model inventing a refund amount (for example 12990 paise instead of 249900), which code rejected every time.

**Part 14: presentation.** Several pieces went in:
- A reviewer-first README that links every claim to the test proving it.
- `docs/architecture.md`, with diagrams and a table of where each design rule is enforced.
- A 3-minute demo recording script with a one-command reset.
- Résumé bullets and an MIT license.
- A docs test that keeps the README's numbers and links from going stale.

Following the README from a clean copy and a brand-new database gave 478 passing tests and identical eval numbers.

**Next (tomorrow):**
- Check that the README diagrams render on GitHub.
- Record the demo and link it in the README.
- Fix the stale `docs/project-guide.md` reference in `CLAUDE.md`.
- Possibly stop asking the model for the refund amount, since code already sets it.

## 2026-10-27

Today the project went from an empty repo to a working, human-approved refund agent: Parts 1 to 9 of the roadmap, 33 commits.

**Parts 1 to 4: the foundation.** These parts work without any AI. The base is a Postgres schema with seed data and a health check (Part 1). JWT authentication was added, so a customer who asks for someone else's order gets a 404 (Part 2). Mock payment, shipping and helpdesk services were built with idempotency and fault injection (Part 3). Refunds can now be resolved deterministically: eligibility is checked in code, amounts are integer paise, and execution is exactly-once (Part 4).

**Parts 5 to 7: the AI layer.** The model reads and suggests; code makes every decision. An LLM layer handles structured output and repairs a bad reply or stops. Read-only tools let the model look things up (Part 5). A LangGraph workflow routes each ticket, and code chooses the route (Part 6). The agent can draft refund proposals, which code checks against the facts before they reach a reviewer queue (Part 7).

**Parts 8 and 9: policies and approval.** Policy search (RAG) runs over versioned policy documents with pgvector and bge-small embeddings, citations are attached to every decision, and a ticket escalates when no policy applies (Part 8). Human approval came last (Part 9):
- A separate reviewer role, with a four-eyes rule so nobody approves their own proposal.
- Durable Postgres checkpoints, so the workflow pauses for approval and resumes even after a restart.
- Proposal expiry after 48 hours.
- Approve, reject and expire paths, checked by restart tests.

**Free local model (in progress).** No Anthropic API key yet, so the agent got a free local backend through Ollama. The work includes a new `OllamaLLM` adapter, a single `LLM_PROVIDER=anthropic|ollama` switch, and 10 adapter tests. The full suite passes: 259 passed, 9 skipped. The `qwen2.5:7b` model download stalled on the slow connection, was restarted, and is still running. Once it finishes, the next steps are the live tests and a real agent run on the local model. Part 10 (async runs with a background worker) comes after that.
