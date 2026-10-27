"""Load policy markdown files into policy_documents / policy_chunks.

One chunk per `## ` section. Each (document, version) is its own set of rows:
ingesting version 2 never touches version 1, so old citations keep resolving.
Re-ingesting unchanged files is a no-op."""

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import PolicyChunk, PolicyDocument
from app.policy.embedder import Embedder

POLICY_DIR = Path(__file__).resolve().parents[2] / "policies"


@dataclass(frozen=True)
class Section:
    ref: str
    heading: str
    text: str


@dataclass(frozen=True)
class PolicyFile:
    id: str
    version: int
    title: str
    effective_from: datetime
    effective_to: datetime | None
    sections: list[Section]
    source_uri: str


def _as_utc(d: date | None) -> datetime | None:
    return None if d is None else datetime.combine(d, time.min, tzinfo=timezone.utc)


def parse_policy(path: Path) -> PolicyFile:
    raw = path.read_text()
    _, front, body = raw.split("---", 2)
    meta = yaml.safe_load(front)
    sections, heading, lines = [], None, []
    for line in body.splitlines() + ["## "]:  # sentinel flushes the last section
        if line.startswith("## "):
            if heading is not None:
                sections.append(Section(f"section_{len(sections) + 1}", heading, "\n".join(lines).strip()))
            heading, lines = line[3:].strip(), []
        elif heading is not None:
            lines.append(line)
    return PolicyFile(id=meta["id"], version=int(meta["version"]), title=meta["title"],
                      effective_from=_as_utc(meta["effective_from"]),
                      effective_to=_as_utc(meta.get("effective_to")),
                      sections=sections, source_uri=f"policies/{path.name}")


def ingest_file(db: Session, pf: PolicyFile, embedder: Embedder) -> bool:
    """Returns True if anything changed. Does not commit."""
    doc = db.get(PolicyDocument, (pf.id, pf.version))
    if doc is None:
        doc = PolicyDocument(id=pf.id, version=pf.version)
        db.add(doc)
    doc.title, doc.source_uri = pf.title, pf.source_uri
    doc.effective_from, doc.effective_to = pf.effective_from, pf.effective_to
    db.flush()

    existing = db.scalars(select(PolicyChunk).where(PolicyChunk.document_id == pf.id,
                                                    PolicyChunk.document_version == pf.version)).all()
    have = {(c.section_ref, c.heading, c.content, c.chunk_metadata.get("embedding_model")) for c in existing}
    want = {(s.ref, s.heading, s.text, embedder.model_name) for s in pf.sections}
    if have == want and all(c.embedding is not None for c in existing):
        return False

    db.execute(delete(PolicyChunk).where(PolicyChunk.document_id == pf.id,
                                         PolicyChunk.document_version == pf.version))
    vectors = embedder.embed_passages([f"{s.heading}\n{s.text}" for s in pf.sections])
    for s, vec in zip(pf.sections, vectors):
        db.add(PolicyChunk(document_id=pf.id, document_version=pf.version, section_ref=s.ref, heading=s.heading,
                           content=s.text, embedding=vec, chunk_metadata={"embedding_model": embedder.model_name}))
    return True


def ingest_dir(db: Session, embedder: Embedder, directory: Path = POLICY_DIR) -> dict[str, bool]:
    results = {}
    for path in sorted(directory.glob("*.md")):
        pf = parse_policy(path)
        results[f"{pf.id}@v{pf.version}"] = ingest_file(db, pf, embedder)
    db.commit()
    return results
