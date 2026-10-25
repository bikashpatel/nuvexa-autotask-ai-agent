"""audit retention purge through an owner-defined SECURITY DEFINER function (PostgreSQL)

Revision ID: c8e4a7d2f1b9
Revises: b5d1f0c3a2e7
Create Date: 2026-10-25 12:00:00.000000+00:00
"""

from collections.abc import Sequence

from alembic import op

from app.db.models import AUDIT_PURGE_FUNCTION_POSTGRES

revision: str = "c8e4a7d2f1b9"
down_revision: str | None = "b5d1f0c3a2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for statement in AUDIT_PURGE_FUNCTION_POSTGRES:
            op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS nuvexa_purge_audit_events(uuid, integer)")
