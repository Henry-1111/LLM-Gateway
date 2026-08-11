"""Add selected provider to request logs.

Revision ID: 20260810_0002
Revises: 20260810_0001
"""
from typing import Optional

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0002"
down_revision: Optional[str] = "20260810_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_logs",
        sa.Column("provider_name", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_request_logs_provider_name",
        "request_logs",
        ["provider_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_request_logs_provider_name", table_name="request_logs")
    op.drop_column("request_logs", "provider_name")
