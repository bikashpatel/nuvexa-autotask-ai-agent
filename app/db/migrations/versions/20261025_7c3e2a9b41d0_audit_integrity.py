"""audit trail integrity: integrity_hash column and append-only triggers

Revision ID: 7c3e2a9b41d0
Revises: e9ce78d91dcb
Create Date: 2026-10-25 08:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.models import AUDIT_APPEND_ONLY_POSTGRES, AUDIT_APPEND_ONLY_SQLITE

revision: str = "7c3e2a9b41d0"
down_revision: str | None = "e9ce78d91dcb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.add_column(sa.Column("integrity_hash", sa.String(length=64), nullable=True))
    statements = AUDIT_APPEND_ONLY_POSTGRES if op.get_bind().dialect.name == "postgresql" else AUDIT_APPEND_ONLY_SQLITE
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events")
        op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS nuvexa_audit_events_guard()")
    else:
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.drop_column("integrity_hash")
