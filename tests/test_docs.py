"""The docs can't rot: every relative link resolves, and the README's headline numbers match the eval report."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


@pytest.mark.parametrize("doc", DOCS, ids=[str(d.relative_to(ROOT)) for d in DOCS])
def test_relative_links_resolve(doc):
    broken = [target for target in LINK.findall(doc.read_text())
              if not target.startswith(("http://", "https://", "mailto:"))
              and not (doc.parent / target).exists()]
    assert broken == []


def test_readme_numbers_match_the_eval_report():
    readme = (ROOT / "README.md").read_text()
    results = json.loads((ROOT / "evals/reports/results.json").read_text())
    pct = lambda v: f"{round(v * 100)}%"  # noqa: E731
    for mode, label in (("single", "Single agent"), ("specialist", "Specialists")):
        r = results[f"ollama:qwen2.5:7b / {mode}"]
        row = next(line for line in readme.splitlines() if line.startswith(f"| {label}"))
        assert pct(r["dev"]["route_acc"]) in row and pct(r["heldout"]["route_acc"]) in row, row
        assert f"{r['all']['mean_tokens']:,}" in row, row
        assert r["all"]["unsafe_proposals"] == 0 and r["all"]["leaks"] == 0
    total_runs = sum(r["all"]["n"] for k, r in results.items() if k.startswith("ollama"))
    assert f"0 unsafe refund proposals in {total_runs} evaluation runs" in readme
