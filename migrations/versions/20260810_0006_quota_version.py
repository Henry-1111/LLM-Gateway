"""Version tenant quota configuration for Redis key rotation.

Revision ID: 20260810_0006
Revises: 20260810_0005
"""
from typing import Optional

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0006"
down_revision: Optional[str] = "20260810_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "quota_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "quota_version")
