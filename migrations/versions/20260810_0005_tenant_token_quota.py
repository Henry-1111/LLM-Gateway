"""Add optional token quota limit to tenants.

Revision ID: 20260810_0005
Revises: 20260810_0004
"""
from typing import Optional

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0005"
down_revision: Optional[str] = "20260810_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("token_quota_limit", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "token_quota_limit")
