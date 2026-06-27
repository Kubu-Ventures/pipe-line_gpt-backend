"""Add invitations table, user status, and mfa_secret

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create user_status enum
    op.execute("CREATE TYPE user_status AS ENUM ('ACTIVE', 'PENDING', 'SUSPENDED')")

    # Add status column to users (default ACTIVE for existing rows)
    op.add_column(
        "users",
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "PENDING", "SUSPENDED", name="user_status"),
            nullable=False,
            server_default="ACTIVE",
        ),
    )

    # Add mfa_secret column to users
    op.add_column(
        "users",
        sa.Column("mfa_secret", sa.String(64), nullable=True),
    )

    # Create invitations table
    op.create_table(
        "invitations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "invited_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_invitations_email", "invitations", ["email"])
    op.create_index("ix_invitations_token", "invitations", ["token"], unique=True)


def downgrade() -> None:
    op.drop_table("invitations")
    op.drop_column("users", "mfa_secret")
    op.drop_column("users", "status")
    op.execute("DROP TYPE user_status")
