# Dev Log

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
