"""Create inbox idempotency and event audit tables.

Revision ID: 20260810_0008
Revises: 20260810_0007
"""
from typing import Optional

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0008"
down_revision: Optional[str] = "20260810_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_events",
        sa.Column("event_id", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_inbox_events_event_type", "inbox_events", ["event_type"])
    op.create_table(
        "event_audit_logs",
        sa.Column("event_id", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["inbox_events.event_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_event_audit_logs_event_type", "event_audit_logs", ["event_type"])
    op.create_index("ix_event_audit_logs_request_id", "event_audit_logs", ["request_id"])
    op.create_index("ix_event_audit_logs_tenant_id", "event_audit_logs", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("event_audit_logs")
    op.drop_table("inbox_events")
