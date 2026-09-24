import uuid
from datetime import datetime
from enum import Enum as PyEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRole(str, PyEnum):
    OPERATOR = "OPERATOR"
    ENGINEER = "ENGINEER"
    ADMIN = "ADMIN"


class UserStatus(str, PyEnum):
    ACTIVE = "ACTIVE"
    PENDING = "PENDING"
    SUSPENDED = "SUSPENDED"


class HITLDecision(str, PyEnum):
    APPROVE = "APPROVE"
    EDIT = "EDIT"
    REJECT = "REJECT"


class QueryStatus(str, PyEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    UNDER_REVIEW = "UNDER_REVIEW"
    DELIVERED = "DELIVERED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class DocumentStatus(str, PyEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        Enum("OPERATOR", "ENGINEER", "ADMIN", name="user_role"), nullable=False, default="OPERATOR"
    )
    status: Mapped[str] = mapped_column(
        Enum("ACTIVE", "PENDING", "SUSPENDED", name="user_status"), nullable=False, default="ACTIVE"
    )
    preferred_lang: Mapped[str] = mapped_column(String(10), default="en")
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    queries: Mapped[list["Query"]] = relationship("Query", back_populates="user")
    hitl_reviews: Mapped[list["HITLReview"]] = relationship("HITLReview", back_populates="reviewer")
    sent_invitations: Mapped[list["Invitation"]] = relationship("Invitation", back_populates="invited_by")


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invited_by: Mapped["User | None"] = relationship("User", back_populates="sent_invitations")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)  # phmsa, pdf, csv, geojson
    sha256_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    ingest_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    operator_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    segment_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    commodity: Mapped[str | None] = mapped_column(String(100), nullable=True)
    year_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(
        Enum("PENDING", "PROCESSING", "COMPLETED", "FAILED", name="document_status"),
        default="PENDING",
    )
    insights_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    chunks: Mapped[list["Chunk"]] = relationship("Chunk", back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    page_ref: Mapped[str | None] = mapped_column(String(50), nullable=True)
    section_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)

    document: Mapped["Document"] = relationship("Document", back_populates="chunks")


class Query(Base):
    __tablename__ = "queries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    question_raw: Mapped[str] = mapped_column(Text, nullable=False)
    question_lang: Mapped[str] = mapped_column(String(10), default="en")
    filters_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    query_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    hitl_required: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(
        Enum("PENDING", "PROCESSING", "UNDER_REVIEW", "DELIVERED", "REJECTED", "FAILED", name="query_status"),
        default="PENDING",
    )

    user: Mapped["User | None"] = relationship("User", back_populates="queries")
    response: Mapped["Response | None"] = relationship("Response", back_populates="query", uselist=False)


class Response(Base):
    __tablename__ = "responses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("queries.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    citations_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence_score: Mapped[float] = mapped_column(Float, default=1.0)
    # HIGH | MEDIUM | LOW from hitl.classify_risk; NULL on rows created before 0005.
    risk_level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    query: Mapped["Query"] = relationship("Query", back_populates="response")
    hitl_review: Mapped["HITLReview | None"] = relationship("HITLReview", back_populates="response", uselist=False)


class HITLReview(Base):
    __tablename__ = "hitl_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    response_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("responses.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    decision: Mapped[str | None] = mapped_column(Enum("APPROVE", "EDIT", "REJECT", name="hitl_decision"), nullable=True)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    response: Mapped["Response"] = relationship("Response", back_populates="hitl_review")
    reviewer: Mapped["User | None"] = relationship("User", back_populates="hitl_reviews")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
