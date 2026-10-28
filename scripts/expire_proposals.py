"""Expire stale proposals now and queue the resumes of their paused agent workflows
(a running worker finishes them). The worker also does this every few minutes on its own.

Run:  uv run python -m scripts.expire_proposals
"""

from datetime import datetime, timezone

from app.agent.resume import request_resume
from app.db.session import SessionLocal
from app.services.approvals import expire_stale

if __name__ == "__main__":
    with SessionLocal() as db:
        expired = expire_stale(db, datetime.now(timezone.utc))
        for pid in expired:
            request_resume(db, pid, "expired")
        db.commit()
    print(f"expired {len(expired)} proposal(s): {', '.join(expired) or '-'}")
