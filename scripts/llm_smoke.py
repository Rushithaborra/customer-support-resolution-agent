"""Try the agent against the real API, the seeded DB and the mock services.

Needs: Postgres seeded, mocks running (uv run uvicorn mocks.main:app --port 8001),
and ANTHROPIC_API_KEY in .env.

Run:  uv run python -m scripts.llm_smoke "I returned my headphones but no refund yet"
      uv run python -m scripts.llm_smoke --as "auth|vikram" "Where is my mouse refund?"
"""

import argparse

from sqlalchemy import select

from app.agent.loop import run_tool_loop
from app.agent.prompts import INVESTIGATE_SYSTEM, TRIAGE_SYSTEM, wrap_customer_message
from app.agent.schemas import TicketTriage
from app.agent.tools import ToolContext
from app.config import settings
from app.db.models import Customer
from app.db.session import SessionLocal
from app.integrations.http import ServiceClient
from app.integrations.shipping import ShippingClient
from app.llm.anthropic_client import AnthropicLLM
from app.llm.structured import structured_call

# $ per million tokens (input, output), for a rough cost line.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("message")
    p.add_argument("--as", dest="subject", default="auth|asha")
    args = p.parse_args()

    llm = AnthropicLLM()
    with SessionLocal() as db, ServiceClient() as http:
        customer = db.scalar(select(Customer).where(Customer.external_auth_subject == args.subject))
        if customer is None:
            raise SystemExit(f"no customer {args.subject}; run the seed script")
        print(f"customer: {customer.id}   model: {settings.llm_model}   effort: {settings.llm_effort}\n")

        triage = structured_call(llm, system=TRIAGE_SYSTEM, model=TicketTriage,
                                 messages=[{"role": "user", "content": wrap_customer_message(args.message)}])
        print("TRIAGE:", triage, "\n")

        ctx = ToolContext(db=db, customer_id=customer.id, shipping=ShippingClient(http))
        result = run_tool_loop(llm, ctx, system=INVESTIGATE_SYSTEM, user_message=args.message)
        for t in result.trace:
            print(f"  tool {t.tool}({t.input}) -> {'ok' if t.ok else t.code}")
        print(f"\nSTOP: {result.stop}   models: {sorted(set(result.models))}")
        print(f"REPLY:\n{result.text}\n")

        usage = triage.usage + result.usage
        price_in, price_out = PRICES.get(settings.llm_model, (0.0, 0.0))
        cost = usage.input_tokens / 1e6 * price_in + usage.output_tokens / 1e6 * price_out
        print(f"tokens: {usage.input_tokens} in / {usage.output_tokens} out   ~${cost:.4f}")


if __name__ == "__main__":
    main()
