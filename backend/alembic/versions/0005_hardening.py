"""query FAILED status, stored response risk level

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A new enum value can't be used in the transaction that adds it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE query_status ADD VALUE IF NOT EXISTS 'FAILED'")
    op.add_column("responses", sa.Column("risk_level", sa.String(10), nullable=True))
    # Queries stranded mid-generation by the old error path can never finish.
    op.execute("UPDATE queries SET status = 'FAILED' WHERE status = 'PROCESSING' AND id NOT IN (SELECT query_id FROM responses)")


def downgrade() -> None:
    op.drop_column("responses", "risk_level")
    op.execute("UPDATE queries SET status = 'REJECTED' WHERE status = 'FAILED'")
    # Postgres cannot drop an enum value; 'FAILED' stays defined but unused.
