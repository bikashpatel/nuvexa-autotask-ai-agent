"""shared rate-limit counters (RATE_LIMIT_BACKEND=database)

Revision ID: b5d1f0c3a2e7
Revises: 7c3e2a9b41d0
Create Date: 2026-10-25 12:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d1f0c3a2e7"
down_revision: str | None = "7c3e2a9b41d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_counters",
        sa.Column("bucket", sa.String(length=64), nullable=False),
        sa.Column("window_index", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("bucket", "window_index", name=op.f("pk_rate_limit_counters")),
    )
    op.create_index("ix_rate_limit_counters_expires_at", "rate_limit_counters", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_rate_limit_counters_expires_at", table_name="rate_limit_counters")
    op.drop_table("rate_limit_counters")
