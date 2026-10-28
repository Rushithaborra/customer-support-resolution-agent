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
