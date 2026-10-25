"""`nuvexa` command line (no servers, migrations or workers are actually started)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import runpy
import signal
import sys
import warnings
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from app import cli
from app.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _plain_argparse_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Python 3.14 argparse colorizes output when FORCE_COLOR is set (as in CI); assert on plain text."""
    monkeypatch.setenv("PYTHON_COLORS", "0")


REPO = Path(cli.__file__).resolve().parent.parent
DEMO_ENV = {
    "DATABASE_URL": "sqlite+aiosqlite:///./data/nuvexa-demo.db",
    "DEMO_MODE": "true",
    "RUN_EMBEDDED_WORKER": "true",
    "LLM_PROVIDER": "heuristic",
    "AZURE_DEVOPS_MODE": "mock",
    "MAIL_MODE": "demo",
    "AZURE_DEVOPS_DEFAULT_PROJECT": "NUVEXA-Demo",
    "LOG_FORMAT": "console",
}


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    return calls


@pytest.fixture
def captured_run(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replaces asyncio.run inside app.cli; closes the coroutine so nothing actually runs."""
    coros: list[Any] = []

    def fake_run(coro: Any) -> None:
        coros.append(coro.cr_code.co_name)
        coro.close()

    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    return coros


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Run in an empty directory with none of the demo variables set; restore everything afterwards."""
    monkeypatch.chdir(tmp_path)
    for name in DEMO_ENV:
        monkeypatch.setenv(name, "placeholder")  # recorded, so values set by the CLI are undone too
        monkeypatch.delenv(name)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_api_defaults(uvicorn_calls: list[Any]) -> None:
    cli.main(["api"])
    ((args, kwargs),) = uvicorn_calls
    assert args == ("app.main:app_factory",)
    assert kwargs == {
        "factory": True,
        "host": "127.0.0.1",
        "port": 8000,
        "reload": False,
        "proxy_headers": True,
        "forwarded_allow_ips": "127.0.0.1",
        "access_log": False,
        "log_config": None,
    }


def test_api_default_host_is_loopback(uvicorn_calls: list[Any]) -> None:
    # The parser default; containers pass --host 0.0.0.0 explicitly.
    cli.main(["api"])
    assert uvicorn_calls[0][1]["host"] == "127.0.0.1"


def test_api_options(uvicorn_calls: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    cli.main(["api", "--host", "0.0.0.0", "--port", "9001", "--reload"])
    kwargs = uvicorn_calls[0][1]
    assert (kwargs["host"], kwargs["port"], kwargs["reload"]) == ("0.0.0.0", 9001, True)
    assert kwargs["forwarded_allow_ips"] == "10.0.0.0/8"


@pytest.mark.parametrize("argv", [[], ["unknown"], ["api", "--port", "not-a-number"], ["demo", "--reload"]])
def test_invalid_arguments_exit_with_usage_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2
    assert "usage: nuvexa" in capsys.readouterr().err


def test_worker_runs_the_worker_main(captured_run: list[str]) -> None:
    cli.main(["worker"])
    assert captured_run == ["_worker_main"]


def test_gen_key_prints_a_valid_fernet_key(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["gen-key"])
    key = capsys.readouterr().out.strip()
    assert Fernet(key.encode()).decrypt(Fernet(key.encode()).encrypt(b"x")) == b"x"
    cli.main(["gen-key"])
    assert capsys.readouterr().out.strip() != key


def test_migrate_runs_alembic_checkpointer_setup_and_runtime_grants(
    monkeypatch: pytest.MonkeyPatch, captured_run: list[str], settings: Settings
) -> None:
    upgrades: list[tuple[Any, str]] = []
    logging_configured: list[tuple[str, str]] = []
    monkeypatch.setattr("alembic.command.upgrade", lambda cfg, rev: upgrades.append((cfg, rev)))
    monkeypatch.setattr("app.observability.logging.configure_logging", lambda *a: logging_configured.append(a))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    cli.main(["migrate"])
    assert logging_configured == [(settings.log_level, settings.log_format)]  # grant events are structured logs
    ((cfg, revision),) = upgrades
    assert revision == "head"
    assert Path(cfg.config_file_name) == REPO / "alembic.ini"
    assert Path(cfg.get_main_option("script_location")) == REPO / "app" / "db" / "migrations"
    assert (REPO / "app" / "db" / "migrations" / "env.py").exists()
    # All DDL happens here, as the schema owner; the runtime role's grants follow once every table exists.
    assert captured_run == ["setup_checkpointer", "grant_runtime_privileges"]


def test_demo_sets_defaults_and_serves_on_loopback(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(cli, "_run_migrations", lambda: calls.append("migrate"))
    monkeypatch.setattr(cli, "_serve_api", lambda host, port, reload=False: calls.append((host, port, reload)))
    cli.main(["demo", "--port", "8123"])
    assert calls == ["migrate", ("127.0.0.1", 8123, False)]
    for name, value in DEMO_ENV.items():
        assert os.environ[name] == value
    assert (isolated_env / "data").is_dir()
    assert get_settings().database_url == DEMO_ENV["DATABASE_URL"]  # cache was cleared after the env change


def test_demo_keeps_explicit_settings(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{isolated_env / 'mine.db'}")
    served: list[Any] = []
    monkeypatch.setattr(cli, "_run_migrations", lambda: None)
    monkeypatch.setattr(cli, "_serve_api", lambda host, port, reload=False: served.append(port))
    cli.main(["demo"])
    assert os.environ["LOG_FORMAT"] == "json"
    assert os.environ["DATABASE_URL"].endswith("mine.db")
    assert served == [8000]


def test_demo_refuses_postgres(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/nuvexa")
    monkeypatch.setattr(cli, "_run_migrations", lambda: pytest.fail("must not migrate"))
    monkeypatch.setattr(cli, "_serve_api", lambda *a, **k: pytest.fail("must not serve"))
    with pytest.raises(SystemExit) as info:
        cli.main(["demo"])
    assert "uses SQLite" in str(info.value.code)


class _FakeWorker:
    def __init__(self) -> None:
        self.ran = False

    def stop(self) -> None: ...

    async def run_forever(self) -> None:
        self.ran = True


async def test_worker_main_wires_signals_and_disposes(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _FakeWorker()
    events: list[Any] = []

    @contextlib.asynccontextmanager
    async def fake_context(db: Any, cfg: Settings) -> AsyncIterator[_FakeWorker]:
        events.append(("context", cfg))
        yield worker

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("app.workers.runner.worker_context", fake_context)
    monkeypatch.setattr("app.observability.logging.configure_logging", lambda *a: events.append(("logging", a)))
    monkeypatch.setattr("app.observability.tracing.configure_tracing", lambda *a: events.append(("tracing", a[1])))
    from app.db.session import Database

    real_dispose = Database.dispose

    async def dispose(self: Database) -> None:
        events.append("dispose")
        await real_dispose(self)

    monkeypatch.setattr(Database, "dispose", dispose)
    loop = asyncio.get_running_loop()
    handlers: dict[int, Any] = {}
    monkeypatch.setattr(loop, "add_signal_handler", handlers.__setitem__)

    await cli._worker_main()

    assert worker.ran
    assert handlers == {signal.SIGINT: worker.stop, signal.SIGTERM: worker.stop}
    assert events == [
        ("logging", (settings.log_level, settings.log_format)),
        ("tracing", "nuvexa-worker"),
        ("context", settings),
        "dispose",
    ]


async def test_worker_main_checks_its_database_privileges_before_starting(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[Any] = []

    async def check(engine: Any, cfg: Settings, *, process: str) -> list[str]:
        events.append(("check", engine, cfg, process))
        return []

    @contextlib.asynccontextmanager
    async def fake_context(db: Any, cfg: Settings) -> AsyncIterator[_FakeWorker]:
        events.append(("context", db.engine))
        yield _FakeWorker()

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("app.db.privileges.check_runtime_privileges", check)
    monkeypatch.setattr("app.workers.runner.worker_context", fake_context)
    monkeypatch.setattr("app.observability.logging.configure_logging", lambda *a: None)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *a: None)

    await cli._worker_main()

    assert [event[0] for event in events] == ["check", "context"]
    (_, engine, cfg, process), (_, context_engine) = events
    assert (cfg, process) == (settings, "worker")
    assert engine is context_engine


async def test_worker_main_disposes_on_failure(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.asynccontextmanager
    async def failing_context(db: Any, cfg: Settings) -> AsyncIterator[_FakeWorker]:
        raise ConnectionError("checkpointer unavailable")
        yield _FakeWorker()  # pragma: no cover

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("app.workers.runner.worker_context", failing_context)
    monkeypatch.setattr("app.observability.logging.configure_logging", lambda *a: None)
    from app.db.session import Database

    disposed: list[bool] = []
    real_dispose = Database.dispose

    async def dispose(self: Database) -> None:
        disposed.append(True)
        await real_dispose(self)

    monkeypatch.setattr(Database, "dispose", dispose)
    with pytest.raises(ConnectionError):
        await cli._worker_main()
    assert disposed == [True]


def test_module_entry_point(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["nuvexa", "gen-key"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # app.cli is already imported
        runpy.run_module("app.cli", run_name="__main__")
    assert Fernet(capsys.readouterr().out.strip().encode())
