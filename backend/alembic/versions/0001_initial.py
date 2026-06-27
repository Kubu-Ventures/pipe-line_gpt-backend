"""Initial schema — all 7 tables + pgvector ivfflat index + RLS on audit_events

Revision ID: 0001
Revises:
Create Date: 2026-06-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── users ────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role", sa.Enum("OPERATOR", "ENGINEER", "ADMIN", name="user_role"), nullable=False, server_default="OPERATOR"),
        sa.Column("preferred_lang", sa.String(10), server_default="en"),
        sa.Column("mfa_enabled", sa.Boolean(), server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # ── documents ────────────────────────────────────────────────────────────
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("sha256_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("ingest_date", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("operator_id", sa.String(100), nullable=True),
        sa.Column("segment_id", sa.String(100), nullable=True),
        sa.Column("commodity", sa.String(100), nullable=True),
        sa.Column("year_from", sa.Integer(), nullable=True),
        sa.Column("year_to", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default="0"),
        sa.Column("status", sa.Enum("PENDING", "PROCESSING", "COMPLETED", "FAILED", name="document_status"), server_default="PENDING"),
    )
    op.create_index("ix_documents_sha256_hash", "documents", ["sha256_hash"])
    op.create_index("ix_documents_operator_id", "documents", ["operator_id"])
    op.create_index("ix_documents_segment_id", "documents", ["segment_id"])

    # ── chunks ───────────────────────────────────────────────────────────────
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0"),
        sa.Column("page_ref", sa.String(50), nullable=True),
        sa.Column("section_label", sa.String(255), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    # ivfflat approximate nearest-neighbour index for fast cosine similarity search
    op.execute(
        "CREATE INDEX ix_chunks_embedding_ivfflat ON chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    # ── queries ──────────────────────────────────────────────────────────────
    op.create_table(
        "queries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", sa.String(100), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("question_raw", sa.Text(), nullable=False),
        sa.Column("question_lang", sa.String(10), server_default="en"),
        sa.Column("filters_json", postgresql.JSONB(), nullable=True),
        sa.Column("query_ts", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("hitl_required", sa.Boolean(), server_default="false"),
        sa.Column("status", sa.Enum("PENDING", "PROCESSING", "UNDER_REVIEW", "DELIVERED", "REJECTED", name="query_status"), server_default="PENDING"),
    )
    op.create_index("ix_queries_session_id", "queries", ["session_id"])
    op.create_index("ix_queries_user_id", "queries", ["user_id"])

    # ── responses ────────────────────────────────────────────────────────────
    op.create_table(
        "responses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("query_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("queries.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("citations_json", postgresql.JSONB(), nullable=True),
        sa.Column("confidence_score", sa.Float(), server_default="1.0"),
        sa.Column("model_version", sa.String(100), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), server_default="0"),
        sa.Column("latency_ms", sa.Integer(), server_default="0"),
    )

    # ── hitl_reviews ─────────────────────────────────────────────────────────
    op.create_table(
        "hitl_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("response_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("responses.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decision", sa.Enum("APPROVE", "EDIT", "REJECT", name="hitl_decision"), nullable=True),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("final_text", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── audit_events ─────────────────────────────────────────────────────────
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=True),
        sa.Column("target_id", sa.String(100), nullable=True),
        sa.Column("target_type", sa.String(100), nullable=True),
        sa.Column("payload_json", postgresql.JSONB(), nullable=True),
        sa.Column("ip_address", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])

    # Row Level Security — prevent DELETE on audit_events
    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY audit_no_delete ON audit_events AS RESTRICTIVE "
        "FOR DELETE TO PUBLIC USING (false)"
    )
    # Allow SELECT and INSERT for all authenticated users
    op.execute(
        "CREATE POLICY audit_select ON audit_events FOR SELECT TO PUBLIC USING (true)"
    )
    op.execute(
        "CREATE POLICY audit_insert ON audit_events FOR INSERT TO PUBLIC WITH CHECK (true)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_events CASCADE")
    op.execute("DROP TABLE IF EXISTS hitl_reviews CASCADE")
    op.execute("DROP TABLE IF EXISTS responses CASCADE")
    op.execute("DROP TABLE IF EXISTS queries CASCADE")
    op.execute("DROP TABLE IF EXISTS chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS documents CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TYPE IF EXISTS hitl_decision")
    op.execute("DROP TYPE IF EXISTS query_status")
    op.execute("DROP TYPE IF EXISTS document_status")
    op.execute("DROP TYPE IF EXISTS user_role")
