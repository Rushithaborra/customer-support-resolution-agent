# Dev Log

## 2026-10-28

Finished the free local model setup and built Part 10 (async runs).

**Local model.** The `qwen2.5:7b` download had stopped before finishing, so it was restarted in the background. The
Ollama adapter code is done and tested. The live run on the local model waits for the download.

**Part 10.** Agent runs no longer happen inside the web request:
- Starting a run, or approving a refund, now records the work as a job in a Postgres table and returns `202 Accepted`
  straight away.
- A separate worker process (`scripts.worker`) picks jobs up with `SELECT … FOR UPDATE SKIP LOCKED`, so several
  workers never grab the same job.
- Each job carries a time-limited lease. If a worker dies, its lease runs out and another worker takes the job over,
  continuing from the run's last LangGraph checkpoint instead of starting again.
- Failures retry with backoff. After 3 attempts a job is dead-lettered and its ticket goes to a human.
- The worker also runs the proposal-expiry sweep every 5 minutes.
- A new `/events` endpoint shows each run's timeline.

**Verified with real processes.** A worker was `kill -9`'d halfway through a run. A second worker took over without
repeating finished steps. After approval, exactly one refund went through. A test also kills the worker right after
the payment provider accepted a refund: the next worker reconciles it by idempotency key, and the provider still has
one refund. **275 tests pass.**

**Part 11 (refund safety).** When a refund call ends without a clear answer (a timeout, a lost response, a crashed
worker), the money may or may not have moved. Until now, only a staff member clicking "refresh" would find out.
- **Automatic sweep.** Every 60 s the worker asks the payment provider about any refund still in that state, using
  the refund's idempotency key. It waits a 60 s grace period first and backs off between attempts.
- **Never guesses.** If the provider has the refund, the real status is recorded. If not, it resends with the same
  key, so a delayed original can't become a second refund.
- **Hands hard cases to a person.** If the provider never answers, or its record disagrees with ours, the refund is
  parked for a human. It stays "unknown", never "failed".
- **Proved.** A test matrix injects a fault at eight different points, and every case ends with exactly one refund.
  Real-thread race tests (parallel executes, two proposals for one order, the sweep racing a slow send) also give one
  refund each.
- **Checked live.** The provider's response was lost on purpose. The customer saw "confirming", and two minutes later
  the sweep settled it with no human involved. **291 tests pass**, and the suite now has zero warnings.

**Part 12 (frontend).** The project now has a browser UI at `/ui`, plain HTML + JavaScript served by the API with no
build step:
- **Customer panel:** Asha picks an order, writes a message, and watches her request's status. The wording always
  comes from the backend's confirmed state.
- **Staff/reviewer console:** a ticket queue and an approval queue, a live workflow timeline (every agent step, tool
  call, pause, worker takeover and refund state), an evidence panel, and a proposal card where only a reviewer can
  approve. Demo buttons make the mock payment provider fail on purpose.
- **Demo model.** With no paid key and a slow local model, a deterministic "demo" model (keyword rules, clearly
  labelled "not an AI") makes the three demos repeat exactly: A (refund end to end), B (cases where it must not
  refund) and C (lost provider response, recovered automatically).
- **Two bugs found and fixed:**
  - Picking the order from the dropdown without typing its id used to escalate the ticket.
  - The internal HTTP client crashed on empty "204" replies.
- **Verified.** All three demos ran against the real API, worker and mocks. **319 tests pass.**

**Part 13 (evaluation).** The agent is now measured, not just tested:
- **The suite.** 60 synthetic cases: 40 for tuning, 20 held back. Each brings its own order data and runs through the
  real agent in a transaction that is rolled back afterwards. Scoring compares against what the system stored, with
  no AI judge.
- **Label check.** Every case is also run with a "perfect model". If code wouldn't reach a label even then, the label
  is wrong and a test fails.
- **Specialist mode.** A separate investigator for refunds, delivery and damage, each with its own prompt and only the
  tools it needs. It is compared against the single agent on the same cases.
- **Results on the local `qwen2.5:7b` model (about 4.5 hours of runs):**
  - The single agent routes 70% of held-out cases correctly; specialists route 85%, using 18% fewer tokens.
  - In both modes there were **zero unsafe refund proposals and zero data leaks**, injection cases included.
  - Every miss was a hand-off to a human. The most common one was the model inventing a refund amount (for example
    12990 paise instead of 249900), which code rejected every time.
- **460 tests pass.**

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
