"""`nuvexa` command line: api, worker, migrate, demo, gen-key."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from app.core.config import get_settings


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "app" / "db" / "migrations"))
    command.upgrade(cfg, "head")

    from app.workflows.checkpointer import setup_checkpointer

    asyncio.run(setup_checkpointer(get_settings()))


def _serve_api(host: str, port: int, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app_factory",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        access_log=False,
        log_config=None,
    )


async def _worker_main() -> None:
    from app.db.session import Database
    from app.observability.logging import configure_logging
    from app.observability.tracing import configure_tracing
    from app.workers.runner import worker_context

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    configure_tracing(settings, f"{settings.otel_service_name}-worker")
    db = Database(settings)
    try:
        async with worker_context(db, settings) as worker:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, worker.stop)
            await worker.run_forever()
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nuvexa", description="NUVEXA-AutoTask-AI-Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    api = sub.add_parser("api", help="Run the HTTP API")
    # Local runs listen on loopback only; containers pass --host 0.0.0.0 explicitly (Dockerfile/compose).
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--reload", action="store_true")
    sub.add_parser("worker", help="Run the background worker")
    sub.add_parser("migrate", help="Apply database migrations and LangGraph checkpoint tables")
    demo = sub.add_parser("demo", help="Single-process local demo (SQLite, embedded worker, mock integrations)")
    demo.add_argument("--port", type=int, default=8000)
    sub.add_parser("gen-key", help="Print a new Fernet key for TOKEN_ENCRYPTION_KEYS")
    args = parser.parse_args(argv)

    match args.command:
        case "api":
            _serve_api(args.host, args.port, args.reload)
        case "worker":
            asyncio.run(_worker_main())
        case "migrate":
            _run_migrations()
        case "gen-key":
            from app.core.security import generate_fernet_key

            print(generate_fernet_key())
        case "demo":
            Path("data").mkdir(exist_ok=True)
            os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/nuvexa-demo.db")
            os.environ.setdefault("DEMO_MODE", "true")
            os.environ.setdefault("RUN_EMBEDDED_WORKER", "true")
            os.environ.setdefault("LLM_PROVIDER", "heuristic")
            os.environ.setdefault("AZURE_DEVOPS_MODE", "mock")
            os.environ.setdefault("MAIL_MODE", "demo")
            os.environ.setdefault("AZURE_DEVOPS_DEFAULT_PROJECT", "NUVEXA-Demo")
            os.environ.setdefault("LOG_FORMAT", "console")
            get_settings.cache_clear()
            if not get_settings().database_url.startswith("sqlite"):
                sys.exit("`nuvexa demo` uses SQLite; unset DATABASE_URL or use `docker compose up` for PostgreSQL.")
            _run_migrations()
            _serve_api("127.0.0.1", args.port)


if __name__ == "__main__":
    main()
