"""Build evals/reports/report.md (+ results.json) from the per-case results in evals/runs/.

    uv run python -m evals.report        # rebuild from existing results, no model calls
"""

import json
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from evals.grade import summarize

ROOT = Path(__file__).resolve().parent
RUNS_DIR, REPORTS_DIR = ROOT / "runs", ROOT / "reports"

NOISE = 0.10  # smallest held-out accuracy gap treated as real (2 of 20 cases)

DISCLAIMER = ("**Synthetic data only.** These cases are invented for this project and run against mock services. "
              "The numbers show how this design behaves on them; they are not production performance.")


def load_rows(runs_dir: Path = RUNS_DIR) -> list[dict]:
    rows = []
    for path in sorted(runs_dir.glob("*.jsonl")):
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return rows


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=ROOT, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _f(v, pct=False) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.0f}%" if pct else str(v)


def _by(rows: list[dict], *keys: str) -> dict:
    out = defaultdict(list)
    for r in rows:
        out[tuple(r[k] for k in keys)].append(r)
    return out


def build(rows: list[dict]) -> tuple[str, dict]:
    runs = _by(rows, "model", "mode")
    results = {}
    for (model, mode), rs in sorted(runs.items()):
        results[f"{model} / {mode}"] = {split: summarize([r for r in rs if split == "all" or r["split"] == split])
                                        for split in ("dev", "heldout", "all")}

    lines = ["# Evaluation report", "", DISCLAIMER, "",
             f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC from commit `{_git_commit()}`. "
             f"Cases: {len({r['case_id'] for r in rows})} "
             f"(dev {len({r['case_id'] for r in rows if r['split'] == 'dev'})}, "
             f"held-out {len({r['case_id'] for r in rows if r['split'] == 'heldout'})}). "
             "Reproduce: `uv run python -m evals.run --model <demo|ollama|anthropic> --mode single,specialist`.", "",
             "Route = what code decided from the model's triage and investigation. Unsafe proposals and leaks "
             "must be 0 for every model: they're guaranteed by code, and measured here to prove it.", ""]

    lines += ["## Results (dev | held-out)", "",
              "| Model / mode | Route acc | Reason acc | Amount acc | Unsafe proposals | Leaks | LLM failures "
              "| Escalation rate | Triage acc | p50 / p95 latency (s) | Mean tokens |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, by_split in results.items():
        d, h = by_split["dev"], by_split["heldout"]
        both = lambda k, pct=True: f"{_f(d[k], pct)} \\| {_f(h[k], pct)}"  # noqa: E731
        lines.append(
            f"| {name} | {both('route_acc')} | {both('reason_acc')} | {both('amount_acc')} "
            f"| {both('unsafe_proposals', False)} | {both('leaks', False)} | {both('llm_failures', False)} "
            f"| {both('escalation_rate')} | {both('triage_acc')} "
            f"| {_f(by_split['all']['latency_p50'])} / {_f(by_split['all']['latency_p95'])} "
            f"| {_f(by_split['all']['mean_tokens'])} |")

    lines += ["", "## Route accuracy by group (all splits)", ""]
    groups = sorted({r["group"] for r in rows})
    lines += ["| Model / mode | " + " | ".join(groups) + " |", "| --- |" + " --- |" * len(groups)]
    for (model, mode), rs in sorted(runs.items()):
        per = _by(rs, "group")
        cells = [f"{_f(summarize(per[(g,)])['route_acc'], True)} ({len(per[(g,)])})" if (g,) in per else "-"
                 for g in groups]
        lines.append(f"| {model} / {mode} | " + " | ".join(cells) + " |")

    lines += ["", "## Single vs specialist", ""]
    compared = False
    for model in sorted({r["model"] for r in rows}):
        s, sp = results.get(f"{model} / single"), results.get(f"{model} / specialist")
        if not (s and sp):
            continue
        compared = True
        for split in ("dev", "heldout"):
            a, b = s[split]["route_acc"], sp[split]["route_acc"]
            if a is None or b is None:
                continue
            delta = round((b - a) * 100)
            lines.append(f"- **{model}, {split}:** single {_f(a, True)} → specialist {_f(b, True)} "
                         f"({'+' if delta >= 0 else ''}{delta} pts; tokens {_f(s[split]['mean_tokens'])} → "
                         f"{_f(sp[split]['mean_tokens'])}).")
        dh = sp["heldout"]["route_acc"], s["heldout"]["route_acc"]
        if None not in dh:
            # At n=20 held-out, one case is 5 points: only a gap of 10+ points (2+ cases) counts.
            verdict = ("specialists help on held-out cases" if dh[0] >= dh[1] + NOISE else
                       "no clear held-out gain: keep the single agent (simpler, fewer prompts)" if dh[0] > dh[1] - NOISE
                       else "specialists are worse on held-out cases")
            lines.append(f"- **Verdict for {model}:** {verdict}.")
    if not compared:
        lines.append("Run both modes for a model to compare them.")

    lines += ["", "## Misses", "", "| Model / mode | Case | Split | Expected | Got |", "| --- | --- | --- | --- | --- |"]
    misses = [r for r in rows if not r["grade"]["reason_ok"] or r["grade"]["unsafe_proposal"] or r["grade"]["leak"]
              or r["grade"]["amount_ok"] is False]
    for r in sorted(misses, key=lambda r: (r["model"], r["mode"], r["case_id"])):
        e, g = r["expect"], r["got"]
        exp = f"{e['route']} ({'/'.join(e['reason']) if isinstance(e['reason'], list) else e['reason']})"
        got = g.get("error") or f"{g.get('route')} ({g.get('reason')})"
        if r["grade"]["amount_ok"] is False:
            got += f", amount {g.get('proposal_amount_minor')} ≠ {e['amount_minor']}"
        if r["grade"]["leak"]:
            got += f", LEAK {r['grade']['leak']}"
        lines.append(f"| {r['model']} / {r['mode']} | `{r['case_id']}` | {r['split']} | {exp} | {got} |")
    if not misses:
        lines.append("| - | none | | | |")
    return "\n".join(lines) + "\n", results


def write_report(runs_dir: Path = RUNS_DIR, out_dir: Path = REPORTS_DIR) -> Path:
    rows = load_rows(runs_dir)
    md, results = build(rows)
    out_dir.mkdir(exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    path = out_dir / "report.md"
    path.write_text(md)
    return path


if __name__ == "__main__":
    print(write_report())
