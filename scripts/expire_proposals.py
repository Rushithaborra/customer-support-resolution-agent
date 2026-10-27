"""Expire stale proposals and finish their paused agent workflows. Safe to run repeatedly
(Part 10's worker will run it on a schedule).

Run:  uv run python -m scripts.expire_proposals
"""

from datetime import datetime, timezone

from app.agent.checkpoint import get_checkpointer
from app.agent.resume import resume_for_proposal
from app.db.session import SessionLocal
from app.integrations.http import ServiceClient
from app.integrations.payment import PaymentClient
from app.services.approvals import expire_stale

if __name__ == "__main__":
    with SessionLocal() as db, ServiceClient() as http:
        expired = expire_stale(db, datetime.now(timezone.utc))
        for pid in expired:
            resume_for_proposal(db, pid, "expired", checkpointer=get_checkpointer(), payment=PaymentClient(http))
    print(f"expired {len(expired)} proposal(s): {', '.join(expired) or '-'}")
