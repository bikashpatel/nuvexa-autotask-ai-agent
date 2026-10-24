"""Runtime hardening: safe checkpoint deserialization, database timeouts, local-only default bind."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import text

from app import cli
from app.core.config import Settings
from app.db.session import Database, postgres_connect_args
from app.workflows.checkpointer import checkpoint_serializer, open_checkpointer
from tests.conftest import make_settings


@dataclasses.dataclass
class Gadget:
    """Stand-in for an attacker-chosen class written into the checkpoint table."""

    command: str


def _malicious_blob() -> tuple[str, bytes]:
    return JsonPlusSerializer(allowed_msgpack_modules=True).dumps_typed(Gadget("touch /tmp/pwned"))


def test_checkpoint_serializer_never_instantiates_unlisted_types() -> None:
    loaded = checkpoint_serializer().loads_typed(_malicious_blob())
    assert not isinstance(loaded, Gadget)


async def test_checkpointer_in_use_is_strict(settings: Settings) -> None:
    async with open_checkpointer(settings) as saver:
        loaded = saver.serde.loads_typed(_malicious_blob())
    assert not isinstance(loaded, Gadget)


def test_postgres_connections_get_statement_and_lock_timeouts() -> None:
    settings = make_settings("postgresql+psycopg://u:p@db:5432/x")
    options = postgres_connect_args(settings)["options"]
    assert f"statement_timeout={settings.database_statement_timeout_ms}" in options
    assert f"lock_timeout={settings.database_lock_timeout_ms}" in options


@pytest.mark.postgres
async def test_statement_timeout_is_enforced_by_postgres(settings: Settings, db: Database) -> None:
    if not settings.is_postgres:
        pytest.skip("requires PostgreSQL")
    async with db.engine.connect() as conn:
        value = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
    assert value != "0"


def test_api_binds_to_localhost_unless_told_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: calls.append(kw))
    cli.main(["api"])
    assert calls[0]["host"] == "127.0.0.1"
    cli.main(["api", "--host", "0.0.0.0"])
    assert calls[1]["host"] == "0.0.0.0"
