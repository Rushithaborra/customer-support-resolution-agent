# Part 14: Presentation (README, architecture, demo kit, résumé, reproducibility)

## What was built

| File | Role |
| --- | --- |
| `README.md` | Rewritten for a reviewer. The pitch, then a "what it proves, and where" table (each claim → its test), the architecture diagram, results, quickstart, repo map, design decisions, limitations and license |
| `docs/architecture.md` | Component diagram, the refund sequence diagram, the refund state machine, and **where each of the 10 rules is enforced and tested** |
| `docs/demo-script.md` | An about 3-minute shot list for recording Demos A, B and C: setup, what to click, one line to say per shot, and how to publish it |
| `scripts/reset_demo.py` | One command between takes: re-seed, clear workflow checkpoints, reset the mock provider if it's running |
| `docs/resume.md` | Résumé bullets (4-line and 2-line versions), the interview one-liner, a 60-second pitch, a follow-up map, and honesty rules |
| `LICENSE` | MIT |
| `tests/test_docs.py` | Every relative link in the README and docs resolves, and the README's headline numbers equal `evals/reports/results.json` |

## How it works

The README is ordered by what a reviewer needs first:
1. **What and why** (pitch, three verified headline numbers).
2. **Proof** (a claim → test table).
3. **How** (diagram).
4. **How well** (eval results with the synthetic-data disclaimer).
5. **Try it** (quickstart, demo, eval).
6. **Where things are** (repo map, links to the part explainers).

The old part-by-part status paragraph moved out. That history lives in `docs/parts/` and `DEVLOG.md`.

Diagrams are Mermaid, so GitHub renders them from text. They can be diffed, reviewed and edited like code, and no
image can go stale silently.

### Reproducibility check (the done-when)

What I did, as a new developer would, on 2026-10-28:
1. Copied exactly the files git would commit into a fresh folder: no `.env`, no `.venv`, no caches.
2. Created a brand-new database (`support_repro`) with the pgvector extension and pointed `.env` at it.
3. Ran the README quickstart in order: `uv sync`, `alembic upgrade head`, `seed_data`, `ingest_policies`, `pytest`,
   `evals.run --model demo`.

| Step | Result |
| --- | --- |
| `uv sync` → migrate → seed → ingest | All succeeded as written |
| `pytest` | **478 passed**, 9 skipped (the opt-in live-LLM tests) |
| `evals.run --model demo` | Metrics **identical** to the committed report, for both modes |
| Ollama rows | Identical, rebuilt from the committed per-case results. The model wasn't re-run (about 4.5 h) |

Afterwards the database was dropped and the copy deleted.

## Why each piece is necessary

- **The claim → test table.** Anyone can write "exactly-once refunds" in a README. Linking each claim to the test
  that fails if it's false turns marketing into something a reviewer can check in one click.
- **The rules → enforcement table** (`docs/architecture.md`). It answers the interview question behind every design
  review: "where is that actually guaranteed?" It names a file and function plus a test, never "the prompt says so".
- **The docs tests.**
  - README numbers are copied from the report by hand, so they drift the moment someone re-runs the eval. The test
    fails if they no longer match.
  - Broken links are the most common sign of an abandoned repo. The link check catches them in CI.
- **`reset_demo` and the shot list.** A recording takes several takes. Demos A and C both refund `ord_1001`, which is
  correctly refused the second time, so without a one-command reset, take two looks like a bug.
- **Honest résumé framing.** The numbers are real, but they come from synthetic data and a small eval. Overclaiming
  ("reduced support costs by…") is the fastest way to lose an interviewer's trust in everything else.
- **A real reproduction, not "it works on my machine".** The fresh-database run is what "another developer
  reproduces the reported results" means, and it's recorded here with what was and wasn't re-run.

## Tradeoffs and known limitations

- **The recording isn't made yet.** The script and reset are ready. The README gets the link when it's recorded.
- **The reproduction used Homebrew Postgres, not Docker.** The README's Docker path wasn't exercised on this machine
  (the `pgvector/pgvector:pg16` image would be pulled fresh). The schema, extension and steps are the same.
- **Ollama numbers were rebuilt from saved results, not re-run.** A full re-run takes about 4.5 hours, and local
  inference isn't guaranteed bit-identical.
- **Mermaid can't be validated locally** without a headless browser. The diagrams use only basic flowchart, sequence
  and state syntax that GitHub renders.
- **`CLAUDE.md` still refers to `docs/project-guide.md`**, which doesn't exist (the guide is
  `docs/project-overview.md`). It's left as is because it's the owner's file.

## Interview questions

**1. How would a reviewer verify your claims without trusting you?**
Every claim in the README links to the test that would fail if it were false. For example, "exactly-once refunds
under failure" links to `test_refund_safety.py`, which injects a fault at 8 points and asserts one provider refund
each time. `docs/architecture.md` maps each design rule to the function that enforces it and a test. And the quickstart
reproduces everything from scratch: I ran it on a fresh database and got 478 passing tests and identical eval metrics.

**2. Your README numbers came from one eval run. How do you keep them honest?**
Three ways. A test compares the README's numbers with `evals/reports/results.json`, so they can't drift. The README
and the report both say "synthetic data, not production performance". And the caveats are in the README itself: 20
held-out cases means one case is 5 points, from one run per mode, which is why specialists aren't the default yet
despite the +15.

**3. If you had another two weeks, what would you build?**
In this order:
1. Stop asking the model for the refund amount, since code already sets it. Wrong-amount drafts were the top failure,
   so I'd fix that on the dev split and re-measure on held-out.
2. Send the drafted replies for non-refund outcomes through the helpdesk.
3. Add provider webhooks, keeping the sweep as the safety net.
4. Run the eval on a stronger hosted model, for a real single-vs-specialist comparison beyond 7B.
