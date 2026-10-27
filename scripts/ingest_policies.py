"""Load policies/*.md into the database with embeddings. Safe to re-run: unchanged files are skipped.

Run:  uv run python -m scripts.ingest_policies
"""

from app.db.session import SessionLocal
from app.policy.embedder import get_embedder
from app.policy.ingest import ingest_dir

if __name__ == "__main__":
    embedder = get_embedder()
    with SessionLocal() as db:
        for doc, changed in ingest_dir(db, embedder).items():
            print(f"  {doc}: {'ingested' if changed else 'unchanged'}")
    print(f"model: {embedder.model_name} ({embedder.dim} dims)")
