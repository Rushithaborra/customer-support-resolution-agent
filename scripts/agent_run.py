"""Run the agent workflow on a real ticket with the real API and print the route and trace.

Needs: Postgres seeded, mocks running (uv run uvicorn mocks.main:app --port 8001),
and ANTHROPIC_API_KEY in .env.

Run:  uv run python -m scripts.agent_run <ticket_id>
      uv run python -m scripts.agent_run --new "I returned my headphones, no refund yet" --order ord_1001
"""

import argparse
import json

from sqlalchemy import select

from app.agent.runner import run_agent
from app.db.models import Customer, Ticket
from app.db.session import SessionLocal
from app.integrations.http import ServiceClient
from app.integrations.shipping import ShippingClient
from app.llm.anthropic_client import AnthropicLLM


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("ticket_id", nargs="?")
    p.add_argument("--new", dest="message", help="create a ticket with this message first")
    p.add_argument("--order", dest="order_id")
    p.add_argument("--as", dest="subject", default="auth|asha")
    args = p.parse_args()

    with SessionLocal() as db, ServiceClient() as http:
        ticket_id = args.ticket_id
        if args.message:
            customer = db.scalar(select(Customer).where(Customer.external_auth_subject == args.subject))
            t = Ticket(customer_id=customer.id, order_id=args.order_id, customer_message=args.message)
            db.add(t)
            db.commit()
            ticket_id = t.id
        if not ticket_id:
            raise SystemExit("give a ticket id or --new")

        run = run_agent(db, AnthropicLLM(), ShippingClient(http), ticket_id, actor="script")
        o = run.outcome or {}
        print(f"ticket {ticket_id}  run {run.id}  status {run.status}")
        print(f"ROUTE: {run.route} ({run.route_reason})   model suggested: {o.get('model_suggestion')}"
              f"   agreed: {o.get('agreed')}")
        print("TRIAGE:", json.dumps(o.get("triage")))
        for t in o.get("trace", []):
            print(f"  tool {t['tool']}({t['input']}) -> {'ok' if t['ok'] else t['code']}")
        if o.get("summary"):
            print("DRAFT REPLY:", o["summary"]["customer_reply_draft"])
        print("usage:", o.get("usage"), " models:", sorted(set(o.get("models") or [])))


if __name__ == "__main__":
    main()
