"""policy chunk embeddings (pgvector), keyword search column, heading

Revision ID: c3e5a7b9d1f2
Revises: b7d2f4a6c8e1
Create Date: 2026-10-27 20:00:00

Vector size 384 = BAAI/bge-small-en-v1.5. Changing the embedding model means a
new migration for the column size and a full re-ingest.

pgvector isn't a "trusted" extension: a superuser must create it once. The
Docker image's user is a superuser; on Homebrew Postgres run, as your superuser:
  psql -d support -c "CREATE EXTENSION IF NOT EXISTS vector"
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "c3e5a7b9d1f2"
down_revision: Union[str, Sequence[str], None] = "b7d2f4a6c8e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("policy_chunks", sa.Column("heading", sa.String(length=200), nullable=False, server_default=""))
    op.add_column("policy_chunks", sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True))
    op.add_column("policy_chunks", sa.Column(
        "search_tsv", postgresql.TSVECTOR(),
        sa.Computed("to_tsvector('english', heading || ' ' || content)", persisted=True), nullable=True))
    op.create_unique_constraint("uq_chunks_doc_version_section", "policy_chunks",
                                ["document_id", "document_version", "section_ref"])
    op.create_index("ix_chunks_embedding", "policy_chunks", ["embedding"], postgresql_using="hnsw",
                    postgresql_ops={"embedding": "vector_cosine_ops"})
    op.create_index("ix_chunks_search_tsv", "policy_chunks", ["search_tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_chunks_search_tsv", table_name="policy_chunks")
    op.drop_index("ix_chunks_embedding", table_name="policy_chunks")
    op.drop_constraint("uq_chunks_doc_version_section", "policy_chunks", type_="unique")
    op.drop_column("policy_chunks", "search_tsv")
    op.drop_column("policy_chunks", "embedding")
    op.drop_column("policy_chunks", "heading")
    # The extension is left installed: dropping it needs a superuser and may be shared.
