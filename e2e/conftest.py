"""System end-to-end harness: black-box tests against a running Docker Compose stack.

Run with `scripts/e2e.sh` (starts an isolated stack, runs these tests, tears it down) or point
E2E_BASE_URL at an already running stack. Each test owns a different demo email so tests are
independent of order. Container-level tests (restarts, database checks) need E2E_COMPOSE_PROJECT.
"""

from __future__ import annotations

import contextlib
import functools
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:28080").rstrip("/")
API = f"{BASE_URL}/api/v1"
COMPOSE_PROJECT = os.environ.get("E2E_COMPOSE_PROJECT")
REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", REPO_ROOT / "e2e-artifacts"))

TERMINAL_RUN = {"completed", "rejected", "cancelled"}
ANALYSIS_IN_FLIGHT = {"received", "normalized", "classified", "extracted", "validated", "action_proposed"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.e2e)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    """Expose the call-phase report to fixtures (UI tests screenshot on failure)."""
    outcome: Any = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


def wait_for(predicate: Callable[[], Any], what: str, timeout: float = 60, interval: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (httpx.HTTPError, KeyError, StopIteration) as exc:
            last_error = exc
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s waiting for {what} (last error: {last_error!r})")


@pytest.fixture(scope="session", autouse=True)
def stack_ready() -> None:
    try:
        wait_for(
            lambda: httpx.get(f"{API}/health/ready", timeout=3).status_code == 200,
            f"{API}/health/ready",
            timeout=float(os.environ.get("E2E_STARTUP_TIMEOUT", "120")),
        )
    except AssertionError as exc:
        pytest.exit(f"The stack is not reachable at {BASE_URL}: {exc}", returncode=3)


class ApiUser:
    """A browser-like API session for one demo persona (cookies + CSRF header)."""

    def __init__(self, persona: str) -> None:
        self.persona = persona
        self.client = httpx.Client(base_url=API, timeout=30, follow_redirects=False)
        resp = self.client.post("/auth/demo-login", json={"persona": persona})
        assert resp.status_code == 200, resp.text
        self.me: dict[str, Any] = resp.json()
        self.csrf = self.client.cookies.get("nuvexa_csrf") or ""

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.client.get(path, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return self.client.post(path, json=json, headers={**self.headers, **kw.pop("headers", {})}, **kw)

    def patch(self, path: str, json: Any = None) -> httpx.Response:
        return self.client.patch(path, json=json, headers=self.headers)

    def put(self, path: str, json: Any = None) -> httpx.Response:
        return self.client.put(path, json=json, headers=self.headers)

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------ workflow helpers

    def emails(self, **params: Any) -> list[dict[str, Any]]:
        resp = self.get("/emails", params={"limit": 100, **params})
        resp.raise_for_status()
        return list(resp.json()["items"])

    def email_by_subject(self, prefix: str) -> dict[str, Any]:
        """Find a demo email by subject prefix, syncing (the demo mailbox delivers mail in batches)."""

        def find() -> dict[str, Any] | None:
            return next((e for e in self.emails() if e["subject"].startswith(prefix)), None)

        for _ in range(6):
            if (found := find()) is not None:
                return found
            before = len(self.emails())
            resp = self.post("/emails/sync", json={})
            assert resp.status_code == 202, resp.text
            # Nothing new within the window means the sync is still running or the mailbox is exhausted.
            with contextlib.suppress(AssertionError):
                wait_for(functools.partial(self._has_more_than, before), "new mail from sync", timeout=10)
        raise AssertionError(f"email with subject prefix {prefix!r} not found")

    def _has_more_than(self, count: int) -> bool:
        return len(self.emails()) > count

    def analyze(self, email: dict[str, Any]) -> Any:
        """Start (or reuse) analysis and wait until the workflow leaves the analysis phase."""
        resp = self.post(f"/emails/{email['id']}/analyze")
        assert resp.status_code in (200, 202), resp.text
        run_id = resp.json()["workflow_run"]["id"]
        return wait_for(
            lambda: (d := self.get(f"/workflows/{run_id}").json())["run"]["status"] not in ANALYSIS_IN_FLIGHT and d,
            f"analysis of {email['subject']!r}",
        )

    def action(self, action_id: str) -> dict[str, Any]:
        resp = self.get(f"/actions/{action_id}")
        resp.raise_for_status()
        return dict(resp.json())

    def wait_action(self, action_id: str, statuses: set[str], timeout: float = 60) -> Any:
        return wait_for(
            lambda: (a := self.action(action_id))["status"] in statuses and a,
            f"action {action_id} in {statuses}",
            timeout=timeout,
        )

    def approve(self, action: dict[str, Any], comment: str | None = None) -> httpx.Response:
        body = {"version": action["version"], "payload_hash": action["payload_hash"], "comment": comment}
        return self.post(f"/actions/{action['id']}/approve", json=body)

    def execute(self, action: dict[str, Any]) -> httpx.Response:
        return self.post(
            f"/actions/{action['id']}/execute",
            json={"version": action["version"], "payload_hash": action["payload_hash"]},
        )


def find_action(detail: dict[str, Any], action_type: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = [
        a for a in detail["actions"] if a["action_type"] == action_type and a["status"] != "superseded"
    ]
    assert matches, f"no {action_type} proposal in {[a['action_type'] for a in detail['actions']]}"
    return matches[0]


@pytest.fixture(scope="session")
def persona_sessions() -> Iterator[dict[str, ApiUser]]:
    """One API session per persona for the whole run (logins are rate limited per client IP)."""
    sessions: dict[str, ApiUser] = {}
    yield sessions
    for user in sessions.values():
        user.close()


@pytest.fixture
def login(persona_sessions: dict[str, ApiUser]) -> Callable[[str], ApiUser]:
    def _login(persona: str) -> ApiUser:
        if persona not in persona_sessions:
            persona_sessions[persona] = ApiUser(persona)
        return persona_sessions[persona]

    return _login


@pytest.fixture
def fresh_login() -> Iterator[Callable[[str], ApiUser]]:
    """A brand-new session (for logout/session tests); closed after the test."""
    users: list[ApiUser] = []

    def _login(persona: str) -> ApiUser:
        user = ApiUser(persona)
        users.append(user)
        return user

    yield _login
    for user in users:
        user.close()


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if not COMPOSE_PROJECT:
        pytest.skip("E2E_COMPOSE_PROJECT is not set; container-level checks are skipped")
    return subprocess.run(
        ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", str(REPO_ROOT / "docker-compose.yml"), *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=180,
    )


def psql(sql: str) -> str:
    result = compose("exec", "-T", "postgres", "psql", "-U", "nuvexa", "-d", "nuvexa", "-tAc", sql)
    return result.stdout.strip()
