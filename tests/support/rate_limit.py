"""Deterministic clocks for the shared (database) rate limiter."""

from __future__ import annotations

from functools import partial
from typing import Any

import pytest

from app import main as main_module
from app.services.rate_limit import DatabaseRateLimiter

# A wall-clock instant that starts a 60-second window (1_800_000_000 / 60 = 30_000_000); most tests count from here.
WINDOW_START = 1_800_000_000.0


class FrozenClock:
    """Injected wall clock: moves only when a test says so."""

    def __init__(self, now: float = WINDOW_START + 10) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def at(self, offset: float) -> None:
        """Jump to `offset` seconds after WINDOW_START."""
        self.now = WINDOW_START + offset


class RecordingLog:
    """Stands in for a module's structlog logger (`monkeypatch.setattr(module, "log", RecordingLog())`).

    structlog's `capture_logs` misses loggers cached before another test reconfigured logging, so tests replace the
    module logger instead (as tests/unit/api/test_errors.py does)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def _record(self, level: str, event: str, **kw: Any) -> None:
        self.events.append({"event": event, "log_level": level, **kw})

    def debug(self, event: str, **kw: Any) -> None:
        self._record("debug", event, **kw)

    def info(self, event: str, **kw: Any) -> None:
        self._record("info", event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._record("warning", event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self._record("error", event, **kw)

    def named(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


def freeze_database_rate_limits(monkeypatch: pytest.MonkeyPatch, clock: FrozenClock) -> None:
    """Every app built by `create_app` from now on gets a database limiter that reads `clock`."""
    monkeypatch.setattr(main_module, "DatabaseRateLimiter", partial(DatabaseRateLimiter, clock=clock))
