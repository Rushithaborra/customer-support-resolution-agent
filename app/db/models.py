"""Database schema.

Design rules enforced here (not in prompts, not in the LLM):
- Money is stored as integers in minor units (paise). Never floats.
- Statuses are constrained to known values at the database level.
- Refund idempotency keys are unique, so the DB itself rejects duplicates.
- Approvals record the hash of the exact proposal they approved.
- Audit events are append-only records of who did what.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id(prefix: str) -> str:
    """Readable prefixed IDs like 'ord_3f9a1c2b7d4e'.
    The prefix makes logs, evidence refs and LLM outputs easy to read."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def status_check(column: str, allowed: list[str], name: str) -> CheckConstraint:
    values = ", ".join(f"'{v}'" for v in allowed)
    return CheckConstraint(f"{column} IN ({values})", name=name)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------- business data


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("cus"))
    # Subject claim from the auth provider (e.g. a JWT 'sub'). Identity comes
    # from here, never from anything the model or customer types.
    external_auth_subject: Mapped[str] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)

    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


ORDER_STATUSES = ["placed", "shipped", "delivered", "cancelled"]


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        status_check("status", ORDER_STATUSES, "ck_orders_status"),
        CheckConstraint("paid_amount_minor >= 0", name="ck_orders_paid_nonneg"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ord"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3))
    paid_amount_minor: Mapped[int] = mapped_column(BigInteger)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")
    returns: Mapped[list["Return"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_qty_pos"),
        CheckConstraint("paid_amount_minor >= 0", name="ck_order_items_paid_nonneg"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("itm"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(64))
    product_name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer)
    paid_amount_minor: Mapped[int] = mapped_column(BigInteger)

    order: Mapped[Order] = relationship(back_populates="items")


RETURN_STATUSES = ["requested", "in_transit", "received", "rejected"]
INSPECTION_STATUSES = ["pending", "passed", "failed"]


class Return(TimestampMixin, Base):
    __tablename__ = "returns"
    __table_args__ = (
        status_check("status", RETURN_STATUSES, "ck_returns_status"),
        status_check("inspection_status", INSPECTION_STATUSES, "ck_returns_inspection"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ret"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inspection_status: Mapped[str] = mapped_column(String(20), default="pending")

    order: Mapped[Order] = relationship(back_populates="returns")


# ---------------------------------------------------------------- support work

TICKET_CATEGORIES = ["order_status", "refund_request", "damaged_item", "unclassified"]
TICKET_STATUSES = ["open", "awaiting_customer", "awaiting_approval", "awaiting_provider", "escalated", "resolved"]


class Ticket(TimestampMixin, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        status_check("category", TICKET_CATEGORIES, "ck_tickets_category"),
        status_check("status", TICKET_STATUSES, "ck_tickets_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("tkt"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    category: Mapped[str] = mapped_column(String(30), default="unclassified")
    status: Mapped[str] = mapped_column(String(30), default="open")
    customer_message: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)


RUN_STATUSES = ["running", "paused", "completed", "failed", "escalated"]
# Chosen by code from code facts, never by the model (Part 6).
AGENT_ROUTES = ["propose_refund", "explain_policy", "report_order_status", "escalate", "order_not_found"]


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        status_check("status", RUN_STATUSES, "ck_runs_status"),
        CheckConstraint("route IS NULL OR route IN (" + ", ".join(f"'{r}'" for r in AGENT_ROUTES) + ")",
                        name="ck_runs_route"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("run"))
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    # LangGraph thread id: links this row to the graph's checkpoints, which
    # live in LangGraph's own tables (kept separate from these business tables).
    thread_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    route: Mapped[str | None] = mapped_column(String(30))
    route_reason: Mapped[str | None] = mapped_column(String(100))
    outcome: Mapped[dict | None] = mapped_column(JSONB)


# ---------------------------------------------------------------- policies (RAG)


class PolicyDocument(Base):
    __tablename__ = "policy_documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "refund_policy"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_uri: Mapped[str] = mapped_column(String(500))


# BAAI/bge-small-en-v1.5. Changing the model means a migration for this size and a re-ingest.
EMBEDDING_DIM = 384


class PolicyChunk(Base):
    __tablename__ = "policy_chunks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("chk"))
    document_id: Mapped[str] = mapped_column(String(64))
    document_version: Mapped[int] = mapped_column(Integer)
    section_ref: Mapped[str] = mapped_column(String(100))  # e.g. "section_3"
    heading: Mapped[str] = mapped_column(String(200), server_default="")
    content: Mapped[str] = mapped_column(Text)
    chunk_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)  # includes embedding_model
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    # Keyword side of hybrid search, maintained by Postgres itself.
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', heading || ' ' || content)", persisted=True))

    __table_args__ = (
        # Composite FK: a chunk belongs to one exact document version.
        ForeignKeyConstraint(
            ["document_id", "document_version"],
            ["policy_documents.id", "policy_documents.version"],
            name="fk_chunks_document_version",
        ),
        UniqueConstraint("document_id", "document_version", "section_ref", name="uq_chunks_doc_version_section"),
        Index("ix_chunks_embedding", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
        Index("ix_chunks_search_tsv", "search_tsv", postgresql_using="gin"),
    )


# ---------------------------------------------------------------- actions & money

PROPOSAL_STATUSES = ["pending_approval", "approved", "rejected", "expired", "superseded", "executed"]


class ActionProposal(TimestampMixin, Base):
    __tablename__ = "action_proposals"
    __table_args__ = (status_check("status", PROPOSAL_STATUSES, "ck_proposals_status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("prp"))
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSONB)
    # SHA-256 of the canonical payload. If anything material changes,
    # the hash changes and the old approval no longer matches.
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending_approval")


APPROVAL_DECISIONS = ["pending", "approved", "rejected"]


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (status_check("decision", APPROVAL_DECISIONS, "ck_approvals_decision"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("apr"))
    proposal_id: Mapped[str] = mapped_column(ForeignKey("action_proposals.id"), index=True)
    approved_payload_hash: Mapped[str] = mapped_column(String(64))
    reviewer_id: Mapped[str | None] = mapped_column(String(64))  # set server-side from reviewer login
    decision: Mapped[str] = mapped_column(String(20), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# reserved: balance held, definitely never sent · submitted: may have been sent
# processing: provider accepted, not settled · pending_reconciliation: outcome unknown
REFUND_STATUSES = ["reserved", "submitted", "processing", "succeeded", "failed", "pending_reconciliation"]
# Statuses that hold part of the order's refundable balance. Only 'failed' releases it.
REFUND_HOLDING_STATUSES = ["reserved", "submitted", "processing", "succeeded", "pending_reconciliation"]


class RefundOperation(TimestampMixin, Base):
    __tablename__ = "refund_operations"
    __table_args__ = (
        status_check("status", REFUND_STATUSES, "ck_refunds_status"),
        CheckConstraint("amount_minor > 0", name="ck_refunds_amount_pos"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("rfd"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("action_proposals.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    # UNIQUE: the database itself refuses a second operation with the same key.
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    provider_reference: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="reserved")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    actor: Mapped[str] = mapped_column(String(64))  # "customer:cus_..", "agent", "reviewer:..", "system"
    event_type: Mapped[str] = mapped_column(String(64))
    redacted_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
