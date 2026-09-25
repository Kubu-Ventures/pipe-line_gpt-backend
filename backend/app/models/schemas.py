import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

# ── Auth ──────────────────────────────────────────────────────────────────────


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    mfa_setup_required: bool = False


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., max_length=256)
    # Required for users with TOTP enrolled; the API answers 401 {"code": "mfa_required"} when missing.
    totp_code: str | None = Field(None, min_length=6, max_length=6, pattern=r"^\d{6}$")


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str
    preferred_lang: str
    mfa_enabled: bool
    created_at: datetime
    last_login: datetime | None = None

    model_config = {"from_attributes": True}


class AcceptInviteRequest(BaseModel):
    token: str
    password: str = Field(..., min_length=12, max_length=256)


class MFASetupResponse(BaseModel):
    provisioning_uri: str
    secret: str


class MFAVerifyRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


# ── Admin / Invitations ───────────────────────────────────────────────────────


class InviteRequest(BaseModel):
    email: EmailStr
    role: str = Field(..., pattern="^(OPERATOR|ENGINEER|ADMIN)$")


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    token: str
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(ACTIVE|PENDING|SUSPENDED)$")


class UserListResponse(BaseModel):
    total: int
    users: list[UserOut]


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
    id: uuid.UUID  # review_id
    response_id: uuid.UUID
    query_id: uuid.UUID
    question_raw: str
    answer_text: str
    confidence_score: float
    citations_json: list[Citation]
    risk_level: str  # HIGH | MEDIUM | LOW
    status: str  # PENDING | APPROVED | REJECTED
    created_at: datetime
    decision: str | None = None

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


# ── Query history ─────────────────────────────────────────────────────────────


class QueryHistoryItem(BaseModel):
    query_id: uuid.UUID
    question: str
    asked_at: datetime
    status: str  # UNDER_REVIEW | DELIVERED | REJECTED
    hitl_required: bool
    answer_text: str  # original AI answer
    final_text: str | None = None  # engineer-approved / edited text
    decision: str | None = None  # APPROVE | EDIT | REJECT
    reason: str | None = None
    reviewed_at: datetime | None = None
    citations: list[Citation] = []
    confidence_score: float


# ── Health ────────────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str
    version: str


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
