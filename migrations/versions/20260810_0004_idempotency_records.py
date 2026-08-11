"""Create tenant-scoped idempotency records.

Revision ID: 20260810_0004
Revises: 20260810_0003
"""
from typing import Optional

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0004"
down_revision: Optional[str] = "20260810_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=True),
        sa.Column("response_status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("response_headers", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["request_id"], ["request_logs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key_hash", name="uq_idempotency_tenant_key"),
    )
    op.create_index(
        "ix_idempotency_records_request_id",
        "idempotency_records",
        ["request_id"],
    )
    op.create_index(
        "ix_idempotency_records_tenant_id",
        "idempotency_records",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
