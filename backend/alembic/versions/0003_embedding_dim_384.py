"""Change embedding dimension from 1536 to 384 for local sentence-transformers model

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-28
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the ivfflat index first (can't alter column with dependent index)
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_ivfflat")
    # Change dimension from 1536 → 384 (all-MiniLM-L6-v2)
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(384)")
    # Recreate the index with the new dimension
    op.execute(
        "CREATE INDEX ix_chunks_embedding_ivfflat ON chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_ivfflat")
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(1536)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_ivfflat ON chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
