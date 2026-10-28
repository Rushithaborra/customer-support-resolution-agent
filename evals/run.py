"""Run the eval cases through the real agent graph and write the report. One command:

    uv run python -m evals.run --model demo --mode single,specialist
    uv run python -m evals.run --model ollama --mode single,specialist --resume     # hours on a laptop
    uv run python -m evals.run --model anthropic --split heldout                    # needs ANTHROPIC_API_KEY

Every case runs in its own rolled-back transaction (fixtures, ticket, run, proposal all vanish),
against in-process mock services and an in-memory checkpointer, so nothing persists. Results
append to evals/runs/<model>-<mode>.jsonl one line per case; --resume skips cases already there.
"""

import argparse
import json
import logging
import time
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.runner import run_agent
from app.db.models import ActionProposal, Customer
from app.db.session import engine
from app.integrations.http import ServiceClient
from app.integrations.shipping import ShippingClient
from app.llm.types import LLM
from app.policy.embedder import get_embedder
from app.services.tickets import create_ticket
from evals.cases import Case, load_cases, now_utc, order_rows, shipment_body
from evals.grade import grade
from mocks.server import running_mocks

RUNS_DIR = Path(__file__).resolve().parent / "runs"
log = logging.getLogger("evals")


def build_model(name: str) -> tuple[LLM, str]:
    """Explicit per name (not LLM_PROVIDER), so one process can't silently eval the wrong model."""
    if name == "demo":
        from app.llm.demo import DemoLLM
        return DemoLLM(delay_s=0), "demo:rules"
    if name == "ollama":
        from app.config import settings
        from app.llm.ollama_client import OllamaLLM
        return OllamaLLM(), f"ollama:{settings.ollama_model}"
    if name == "anthropic":
        from app.config import settings
        from app.llm.anthropic_client import AnthropicLLM
        return AnthropicLLM(), f"anthropic:{settings.llm_model}"
    raise SystemExit(f"unknown model {name!r}")


def run_case(case: Case, llm: LLM, mode: str, http: ServiceClient) -> dict:
    """One case, fully isolated. Returns what the system did (not yet graded)."""
    now = now_utc()
    http.call("POST", "/_admin/reset")
    for fx in case.fixtures:
        if body := shipment_body(fx, now):
            http.call("POST", f"/_admin/shipping/shipments/{fx.id}", json=body)

    conn = engine.connect()
    outer = conn.begin()
    db = Session(bind=conn, join_transaction_mode="create_savepoint", autoflush=False, expire_on_commit=False)
    started = time.monotonic()
    try:
        for fx in case.fixtures:
            rows = order_rows(fx, now)
            db.add(rows[0])
            db.flush()
            db.add_all(rows[1:])
        db.commit()
        customer = db.get(Customer, case.customer_id)
        ticket = create_ticket(db, customer, case.message, case.form_order)
        run = run_agent(db, llm, ShippingClient(http), ticket.id, actor="eval", embedder=get_embedder(),
                        checkpointer=InMemorySaver(), mode=mode)
        proposal = db.scalars(select(ActionProposal).where(ActionProposal.run_id == run.id)).first()
        o = run.outcome or {}
        return {"route": run.route, "reason": run.route_reason, "status": run.status,
                "triage": o.get("triage"), "agreed": o.get("agreed"),
                "proposal_amount_minor": proposal.payload["amount_minor"] if proposal else None,
                "tool_calls": [t["tool"] for t in o.get("trace", [])], "models": sorted(set(o.get("models") or [])),
                "tokens": o.get("usage") or {"input_tokens": 0, "output_tokens": 0},
                "outcome": {k: o.get(k) for k in ("summary", "proposal_draft", "proposal_violations",
                                                  "model_suggestion", "citations")},
                "latency_s": round(time.monotonic() - started, 2), "error": None}
    except Exception as e:  # noqa: BLE001 - a crashing case is a result, not the end of the run
        log.exception("case %s crashed", case.id)
        return {"route": None, "reason": None, "error": f"{type(e).__name__}: {e}"[:300],
                "latency_s": round(time.monotonic() - started, 2), "tokens": None}
    finally:
        db.close()
        outer.rollback()  # fixtures, ticket, run, proposal, audit rows: all gone
        conn.close()


def results_path(model: str, mode: str) -> Path:
    return RUNS_DIR / f"{model}-{mode}.jsonl"


def done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(line)["case_id"] for line in path.read_text().splitlines() if line.strip()}


def run_all(cases: list[Case], model: str, modes: list[str], *, resume: bool, mocks_url: str | None = None) -> None:
    llm, model_id = build_model(model)
    RUNS_DIR.mkdir(exist_ok=True)

    def go(url: str) -> None:
        with ServiceClient(base_url=url) as http:
            for mode in modes:
                path = results_path(model, mode)
                if not resume and path.exists():
                    path.unlink()
                skip = done_ids(path)
                todo = [c for c in cases if c.id not in skip]
                log.info("%s / %s: %d cases to run (%d already done)", model_id, mode, len(todo), len(skip))
                for i, case in enumerate(todo, 1):
                    got = run_case(case, llm, mode, http)
                    row = {"case_id": case.id, "group": case.group, "split": case.split, "tags": case.tags,
                           "model": model_id, "mode": mode, "expect": case.expect.model_dump(),
                           "got": got, "grade": grade(case, got), "latency_s": got.get("latency_s"),
                           "tokens": got.get("tokens")}
                    with path.open("a") as f:
                        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                    mark = "ok " if row["grade"]["route_ok"] else "MISS"
                    log.info("[%s %d/%d] %s %-12s expect %-20s got %s (%s) %.1fs", mode, i, len(todo), mark, case.id,
                             case.expect.route, got.get("route"), got.get("reason"), got.get("latency_s") or 0)

    if mocks_url:
        go(mocks_url)
    else:
        with running_mocks() as url:
            go(url)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="demo", choices=["demo", "ollama", "anthropic"])
    p.add_argument("--mode", default="single,specialist", help="comma-separated: single, specialist")
    p.add_argument("--split", default="all", choices=["all", "dev", "heldout"])
    p.add_argument("--case", action="append", help="run only this case id (repeatable)")
    p.add_argument("--limit", type=int, help="run only the first N cases")
    p.add_argument("--resume", action="store_true", help="keep existing results and skip cases already run")
    p.add_argument("--no-report", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cases = [c for c in load_cases() if args.split == "all" or c.split == args.split]
    if args.case:
        cases = [c for c in cases if c.id in args.case]
    if args.limit:
        cases = cases[:args.limit]
    modes = [m.strip() for m in args.mode.split(",") if m.strip()]
    run_all(cases, args.model, modes, resume=args.resume)
    if not args.no_report:
        from evals.report import write_report
        log.info("report: %s", write_report())


if __name__ == "__main__":
    main()
