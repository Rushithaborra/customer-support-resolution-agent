"""Run the background worker: agent runs, approval resumes and the expiry sweep.

Needs: Postgres, the mocks (uv run uvicorn mocks.main:app --port 8001), and an LLM
(ANTHROPIC_API_KEY in .env, or LLM_PROVIDER=ollama). Run several for parallelism.

Run:  uv run python -m scripts.worker            # until Ctrl-C (finishes the current job first)
      uv run python -m scripts.worker --once     # run every job that's due, then exit
"""

import argparse
import logging
import signal
import threading

from app.agent.checkpoint import get_checkpointer
from app.config import settings
from app.db.session import SessionLocal
from app.integrations.http import ServiceClient
from app.integrations.payment import PaymentClient
from app.integrations.shipping import ShippingClient
from app.jobs.worker import WorkerContext, default_worker_id, drain, serve
from app.llm.deps import build_llm, describe_llm
from app.policy.embedder import get_embedder


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="drain due jobs and exit")
    p.add_argument("--poll", type=float, default=1.0, help="seconds between polls when idle")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    worker_id = default_worker_id()
    with ServiceClient() as http:
        ctx = WorkerContext(llm=build_llm(), shipping=ShippingClient(http), payment=PaymentClient(http),
                            embedder=get_embedder(), checkpointer=get_checkpointer(), mode=settings.agent_mode)
        logging.info("worker %s up, model: %s, agent mode: %s", worker_id, describe_llm(), settings.agent_mode)
        if args.once:
            with SessionLocal() as db:
                jobs = drain(db, ctx, worker_id)
            logging.info("ran %d job(s)", len(jobs))
            return
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        serve(SessionLocal, ctx, worker_id, stop, poll_s=args.poll)
        logging.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
