"""Create provider attempt records for failover.

Revision ID: 20260810_0003
Revises: 20260810_0002
"""
from typing import Optional

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0003"
down_revision: Optional[str] = "20260810_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("provider_name", sa.String(length=100), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["request_id"], ["request_logs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id",
            "attempt_number",
            name="uq_attempt_request_number",
        ),
    )
    op.create_index(
        "ix_provider_attempts_provider_name",
        "provider_attempts",
        ["provider_name"],
    )
    op.create_index(
        "ix_provider_attempts_request_id",
        "provider_attempts",
        ["request_id"],
    )


def downgrade() -> None:
    op.drop_table("provider_attempts")
