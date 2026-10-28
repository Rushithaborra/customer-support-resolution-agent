"""The one-command runner: results per case, resume, and a report with every section."""

import json

from evals import report as report_mod
from evals import run as run_mod
from evals.cases import load_cases


def test_runner_writes_results_resumes_and_reports(tmp_path, monkeypatch, mock_server_url, mock_admin):
    monkeypatch.setattr(run_mod, "RUNS_DIR", tmp_path / "runs")
    cases = [c for c in load_cases() if c.id in ("elig-01", "refuse-01", "notfound-01", "elig-08")]
    run_mod.run_all(cases, "demo", ["single", "specialist"], resume=False, mocks_url=mock_server_url)

    single = (tmp_path / "runs" / "demo-single.jsonl").read_text().splitlines()
    assert len(single) == 4
    row = json.loads(single[0])
    assert {"case_id", "split", "group", "expect", "got", "grade", "latency_s"} <= set(row)

    run_mod.run_all(cases, "demo", ["single"], resume=True, mocks_url=mock_server_url)  # nothing left to do
    assert len((tmp_path / "runs" / "demo-single.jsonl").read_text().splitlines()) == 4

    path = report_mod.write_report(tmp_path / "runs", tmp_path / "reports")
    md = path.read_text()
    for section in ("Synthetic data only", "## Results (dev | held-out)", "## Route accuracy by group",
                    "## Single vs specialist", "## Misses", "demo:rules / single", "demo:rules / specialist"):
        assert section in md, section
    results = json.loads((tmp_path / "reports" / "results.json").read_text())
    assert results["demo:rules / single"]["all"]["unsafe_proposals"] == 0
