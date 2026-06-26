import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field


# ── Auth ──────────────────────────────────────────────────────────────────────

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    role: str = "OPERATOR"
    preferred_lang: str = "en"


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    preferred_lang: str
    mfa_enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Query ─────────────────────────────────────────────────────────────────────

class QueryFilters(BaseModel):
    pipeline_segment: str | None = None
    date_range: tuple[int, int] | None = None
    commodity: str | None = None


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4096)
    language: str | None = None
    session_id: str
    filters: QueryFilters | None = None


class Citation(BaseModel):
    source_id: str
    document_id: str
    filename: str
    chunk_index: int
    page_ref: str | None
    section_label: str | None
    excerpt: str


class QuerySSEChunk(BaseModel):
    delta: str
    citations: list[Citation]
    hitl_required: bool
    query_id: str


# ── Ingest ────────────────────────────────────────────────────────────────────

class IngestResponse(BaseModel):
    task_id: str
    document_id: str
    filename: str
    message: str


class IngestStatusResponse(BaseModel):
    task_id: str
    status: str
    document_id: str | None = None
    chunk_count: int | None = None
    dedup_skipped: bool = False


# ── Review (HITL) ─────────────────────────────────────────────────────────────

class ReviewQueueItem(BaseModel):
    review_id: uuid.UUID
    response_id: uuid.UUID
    query_id: uuid.UUID
    question: str
    ai_answer: str
    confidence_score: float
    citations: list[Citation]
    source_chunks: list[dict[str, Any]]
    created_at: datetime

    model_config = {"from_attributes": True}


class ReviewDecisionRequest(BaseModel):
    decision: str  # APPROVE | EDIT | REJECT
    final_text: str | None = None
    reason: str | None = None


class ReviewDecisionResponse(BaseModel):
    review_id: uuid.UUID
    decision: str
    message: str


# ── Audit ─────────────────────────────────────────────────────────────────────

class AuditEventOut(BaseModel):
    id: uuid.UUID
    event_type: str
    actor_id: str | None
    target_id: str | None
    target_type: str | None
    payload_json: dict | None
    ip_address: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[AuditEventOut]


# ── Health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str
    version: str = "0.1.0"


# ── Documents ─────────────────────────────────────────────────────────────────

class DocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    source_type: str
    ingest_date: datetime
    chunk_count: int
    status: str
    dedup_skipped: bool = False

    model_config = {"from_attributes": True}
