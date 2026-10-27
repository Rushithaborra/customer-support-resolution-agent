"""Hybrid, versioned, cited policy retrieval.

1. Only policy versions in effect at `as_of` are searched.
2. Vector search (cosine) and keyword search (Postgres full text) each rank candidates;
   reciprocal-rank fusion combines them. Keywords help exact terms ("inspection", "30 days").
3. A cosine-similarity cutoff drops anything not actually about the query. Keyword rank
   can reorder results but can't rescue an unrelated chunk. Empty result = no policy.
"""

from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import PolicyChunk, PolicyDocument
from app.policy.embedder import Embedder

RRF_K = 60  # standard reciprocal-rank-fusion constant
CANDIDATES = 10


@dataclass(frozen=True)
class Citation:
    document_id: str
    version: int
    section_ref: str
    heading: str
    text: str
    similarity: float

    @property
    def ref(self) -> str:
        return f"{self.document_id}@v{self.version}#{self.section_ref}"

    def as_dict(self) -> dict:
        return {"ref": self.ref, **asdict(self)}


def _in_effect(as_of: datetime):
    return and_(PolicyDocument.id == PolicyChunk.document_id,
                PolicyDocument.version == PolicyChunk.document_version,
                PolicyDocument.effective_from <= as_of,
                or_(PolicyDocument.effective_to.is_(None), PolicyDocument.effective_to > as_of))


def retrieve(db: Session, query: str, *, embedder: Embedder, as_of: datetime, k: int = 3,
             min_similarity: float | None = None) -> list[Citation]:
    cutoff = settings.policy_min_similarity if min_similarity is None else min_similarity
    qvec = embedder.embed_query(query)
    distance = PolicyChunk.embedding.cosine_distance(qvec)

    # Vector side: nearest chunks with their similarity.
    vec_rows = db.execute(
        select(PolicyChunk, (1 - distance).label("sim")).join(PolicyDocument, _in_effect(as_of))
        .where(PolicyChunk.embedding.is_not(None)).order_by(distance).limit(CANDIDATES)
    ).all()
    sims = {chunk.id: float(sim) for chunk, sim in vec_rows}
    chunks = {chunk.id: chunk for chunk, _ in vec_rows}
    vec_rank = {chunk.id: i for i, (chunk, _) in enumerate(vec_rows)}

    # Keyword side: any query term may match (OR), ranked by ts_rank.
    tsq = func.to_tsquery("english", func.replace(cast(func.plainto_tsquery("english", query), String), "&", "|"))
    kw_rows = db.execute(
        select(PolicyChunk, (1 - distance).label("sim")).join(PolicyDocument, _in_effect(as_of))
        .where(PolicyChunk.embedding.is_not(None), PolicyChunk.search_tsv.op("@@")(tsq))
        .order_by(func.ts_rank(PolicyChunk.search_tsv, tsq).desc()).limit(CANDIDATES)
    ).all()
    kw_rank = {}
    for i, (chunk, sim) in enumerate(kw_rows):
        kw_rank[chunk.id] = i
        sims.setdefault(chunk.id, float(sim))
        chunks.setdefault(chunk.id, chunk)
    db.commit()  # end the read transaction

    def fused(cid: str) -> float:
        return sum(1 / (RRF_K + r[cid] + 1) for r in (vec_rank, kw_rank) if cid in r)

    keep = [cid for cid in chunks if sims[cid] >= cutoff]
    keep.sort(key=fused, reverse=True)
    return [Citation(chunks[c].document_id, chunks[c].document_version, chunks[c].section_ref,
                     chunks[c].heading, chunks[c].content, round(sims[c], 4)) for c in keep[:k]]
