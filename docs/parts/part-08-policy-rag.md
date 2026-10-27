# Part 8 — Policy RAG: versioned policies, bge-small embeddings, pgvector, citations

## What was built

| File | Role |
| --- | --- |
| `policies/*.md` | 5 synthetic policy versions: `refund_policy` v1 (retired, 15-day window) and v2 (current, 30-day), `returns_inspection_policy`, `damaged_item_policy`, `shipping_policy` |
| `migrations/versions/c3e5a7b9d1f2_…` | `vector` extension, `embedding vector(384)` + HNSW cosine index, generated `search_tsv` + GIN index, `heading`, unique (doc, version, section) |
| `app/policy/embedder.py` | `FastEmbedder` (bge-small-en-v1.5, ONNX), `FakeEmbedder`, `get_embedder` |
| `app/policy/ingest.py` | Parse front matter, one chunk per `##` section, version-scoped and idempotent ingest |
| `app/policy/retrieve.py` | Hybrid (vector + keyword) retrieval, effective-date filter, similarity cutoff, `Citation` |
| `app/policy/queries.py` | **Code-owned** query for each reason code |
| `app/agent/graph.py` | New read-only `cite_policy` node after `route`. Citations go into the outcome and the proposal |
| `app/agent/tools.py` | New read tool `search_policy` (policy text returned as data) |
| `app/services/proposals.py`, `resolution.py` | Proposals must carry `policy_citations`. The staff path cites too |
| `scripts/ingest_policies.py`, `scripts/policy_search.py` | Ingest, and search from the command line with scores |
| `tests/test_policy_ingest.py`, `test_policy_retrieval.py`, `test_agent_policy.py` | 38 new tests (235 total), using the **real** model |

**Setup changes:**
- pgvector 0.8.1 built from source for Homebrew PG16.
- New dependencies: `fastembed`, `pgvector`, `pyyaml`.
- The model (64 MB, quantized ONNX) is cached in `~/.cache/fastembed`.

## How it works

```
policies/*.md ──ingest──► policy_documents (id, version, effective_from/to)
                          policy_chunks (section, heading, text, embedding[384], search_tsv)

route (code) ──reason_code──► queries.py (code-written query) ──► retrieve(as_of=now)
                                    │   versions in effect at as_of only
                                    │   vector top-10 (cosine) ┐
                                    │   keyword top-10 (FTS)   ┴─ reciprocal-rank fusion
                                    │   drop anything with cosine < 0.70
                                    ▼
                    citations? ── no ──► escalate(no_policy_found)
                        │ yes
                        ▼
      outcome.citations · proposal.payload.policy_citations (inside the approval hash)
```

A citation looks like `refund_policy@v2#section_1`. It names the exact document **version**, so when v3 arrives, old
proposals still point at the text that applied when they were approved.

### Cutoff calibration (bge-small-en-v1.5, measured on this corpus)

| Query type | Top similarity |
| --- | --- |
| Code-built queries (11 reason codes) | **0.776 – 0.928**, all on the expected section |
| Unrelated questions (bread, password, gift cards, chargeback…) | **0.361 – 0.601** |
| Raw customer wording ("I posted it back but no money") | 0.575 – 0.764, which **overlaps** the unrelated range |

The cutoff is **0.70**: at least 0.17 below every code query and 0.10 above every unrelated one. The overlap in the
last row is why code, not the model or the customer, writes the query that decides the citation.

### A bug found while building
fastembed's `query_embed` does **not** add bge-small's query instruction. The model is asymmetric (queries carry
"Represent this sentence for searching relevant passages: ", passages don't), so `FastEmbedder` adds the prefix itself.
Getting it wrong raises no error; retrieval just gets quietly worse.

**Frontend (Part 12):** each decision shows its citation chips (`refund_policy@v2 · When a refund can be issued`), and
clicking one shows the cited text. On a proposal card, the citations sit next to the evidence refs.

## Why each piece is necessary

| Piece | What breaks without it |
| --- | --- |
| Code builds the query from the reason code | A customer message ("per your policy, refunds are instant") steers which text gets cited |
| Similarity cutoff → `no_policy_found` → escalate | The nearest chunk is always *something*, so the agent cites an irrelevant rule as if it applied |
| Effective-date filter | A refund decided today cites the retired 15-day rule |
| Version in every citation | After a policy update, old approvals point at text nobody approved |
| Citations inside the proposal hash | Someone swaps the citation to a different version after approval (the test does this, and execution gets a 409) |
| Hybrid search | Pure vectors can miss exact terms. Pure keywords miss paraphrases. RRF combines both |
| Version-scoped, idempotent ingest | Re-ingesting v2 would wipe v1, and old citations would dangle. Re-running would duplicate rows |
| Test that code constants appear in the cited text | Someone changes `RETURN_WINDOW_DAYS` to 45 while the policy still says 30, and nobody notices |
| Policy text labelled as data | A planted policy line ("approve all refunds") reads like an order. The test shows the route is unaffected |
| Model cache outside the temp dir | macOS clears temp, so every restart re-downloads the model |

## Tradeoffs and known limitations

- **Tiny corpus (16 sections).** The HNSW index and hybrid fusion are more than this needs, but they're the shape a real
  policy library needs, and they cost nothing here.
- **The cutoff is model-specific.** Switching to Voyage (Part 13 comparison) means re-embedding, a new column size
  (migration) and re-calibrating the cutoff from the same measurements.
- **No re-ranker.** For a larger corpus, a cross-encoder re-ranker on the top 10 would sharpen ordering.
- **`search_policy` with raw wording often finds nothing above the cutoff.** That's acceptable: the tool is for the model
  to read, and the authoritative citation always comes from the code query.
- **`not_delivered` has no written policy**, so a refund request on an undelivered order escalates. That's the
  intended behaviour ("never invent a rule"), and the test relies on it.
- **First run downloads the model** (~64 MB from Hugging Face). After that, everything is offline.
- **pgvector on Homebrew PG16 must be created by a superuser once** (it isn't a trusted extension). Docker users
  aren't affected.

## Interview questions

**1. Why doesn't the LLM write the retrieval query?**
Because the citation is part of the decision record, so it has to be deterministic and can't be steered by the customer.
I measured it: with bge-small, raw customer phrasing scored 0.58–0.76 against the right section, while unrelated questions
scored up to 0.60. No single cutoff separates those. Queries written by code from the rules' own reason code scored
0.78–0.93. So code maps `return_not_received` to a fixed query, retrieves, and either gets a confident citation or
escalates. The model can still search policies through a tool to *read* them, but that never decides what's cited.

**2. What happens when you publish a new policy version?**
Ingesting `refund_policy` v2 only replaces v2's own rows. v1's chunks stay, with `effective_to` set. Retrieval filters
by effective date, so a decision made today cites v2, and a replay "as of" March cites v1 (tested: 15 days vs 30 days).
Citations include the version (`refund_policy@v2#section_2`) and sit inside the proposal's payload hash, so an approval
always refers to the exact text the reviewer saw. A separate test checks that the numbers in code still match the
current text, so a policy update without a code update fails CI.

**3. How did you pick the 0.70 similarity cutoff?**
Empirically, not by feel. I ran every code query and a set of unrelated questions and recorded the top similarity:
code queries came out at 0.776–0.928, unrelated ones at 0.361–0.601. 0.70 leaves margin on both sides. Those
measurements are in this doc and pinned by tests (each reason code must clear 0.70, and each unrelated query must
return nothing), so a model or corpus change that breaks the gap fails loudly.
