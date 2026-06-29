"""add insights_json to documents

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("insights_json", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "insights_json")
