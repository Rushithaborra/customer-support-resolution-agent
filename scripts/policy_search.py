"""Search the policies like the agent does, and print ranked citations with scores.

Run:  uv run python -m scripts.policy_search "my return hasn't arrived yet"
      uv run python -m scripts.policy_search --as-of 2026-03-01 "return window"
      uv run python -m scripts.policy_search --all "bake bread"     # ignore the cutoff
"""

import argparse
from datetime import datetime, timezone

from app.config import settings
from app.db.session import SessionLocal
from app.policy.embedder import get_embedder
from app.policy.retrieve import retrieve

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("query")
    p.add_argument("--as-of", help="YYYY-MM-DD (default: now)")
    p.add_argument("--all", action="store_true", help="show results below the similarity cutoff too")
    args = p.parse_args()

    as_of = (datetime.fromisoformat(args.as_of).replace(tzinfo=timezone.utc) if args.as_of
             else datetime.now(timezone.utc))
    with SessionLocal() as db:
        hits = retrieve(db, args.query, embedder=get_embedder(), as_of=as_of, k=5,
                        min_similarity=-1.0 if args.all else None)
    print(f"cutoff {settings.policy_min_similarity}   as of {as_of.date()}")
    for c in hits:
        print(f"  {c.similarity:.3f}  {c.ref:<40} {c.heading}")
    if not hits:
        print("  (no policy found)")
