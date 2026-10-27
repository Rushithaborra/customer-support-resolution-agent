"""Policy files become versioned, embedded sections; re-ingesting is a no-op; versions coexist."""

import math
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db.models import PolicyChunk
from app.db.session import SessionLocal
from app.policy.embedder import get_embedder
from app.policy.ingest import POLICY_DIR, ingest_dir, ingest_file, parse_policy


def test_parse_sections_and_dates():
    v2 = parse_policy(POLICY_DIR / "refund_policy_v2.md")
    assert (v2.id, v2.version) == ("refund_policy", 2)
    assert [s.ref for s in v2.sections] == [f"section_{i}" for i in range(1, 6)]
    assert v2.sections[1].heading == "Return window" and "30 days" in v2.sections[1].text
    assert v2.effective_from == datetime(2026, 7, 1, tzinfo=timezone.utc) and v2.effective_to is None
    v1 = parse_policy(POLICY_DIR / "refund_policy_v1.md")
    assert v1.effective_to == datetime(2026, 7, 1, tzinfo=timezone.utc)  # v1 ends exactly when v2 starts


def test_reingest_is_a_noop():
    with SessionLocal() as s:  # the session fixture already ingested everything
        assert not any(ingest_dir(s, get_embedder()).values())


def test_versions_coexist(db):
    counts = dict(db.execute(select(PolicyChunk.document_version, func.count())
                             .where(PolicyChunk.document_id == "refund_policy")
                             .group_by(PolicyChunk.document_version)).all())
    assert counts == {1: 4, 2: 5}


def test_vectors_are_384_dims_and_normalized(db):
    chunk = db.scalar(select(PolicyChunk).where(PolicyChunk.document_id == "refund_policy").limit(1))
    vec = list(chunk.embedding)
    assert len(vec) == 384
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, abs_tol=1e-4)
    assert chunk.chunk_metadata["embedding_model"] == "BAAI/bge-small-en-v1.5"


def test_changing_v2_leaves_v1_untouched(db):
    v1_ids = set(db.scalars(select(PolicyChunk.id).where(PolicyChunk.document_id == "refund_policy",
                                                         PolicyChunk.document_version == 1)).all())
    v2 = parse_policy(POLICY_DIR / "refund_policy_v2.md")
    edited = replace(v2, sections=[replace(v2.sections[0], text=v2.sections[0].text + " Edited.")] + v2.sections[1:])
    assert ingest_file(db, edited, get_embedder()) is True
    after = set(db.scalars(select(PolicyChunk.id).where(PolicyChunk.document_id == "refund_policy",
                                                        PolicyChunk.document_version == 1)).all())
    assert after == v1_ids  # old citations still resolve
    db.rollback()
