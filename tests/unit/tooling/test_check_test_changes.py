"""TDD guard: production code changes must come with test changes (scripts/check_test_changes.py)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_test_changes.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_test_changes", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    return _load()


@pytest.mark.parametrize(
    "changed",
    [
        ["app/services/execution_service.py", "tests/unit/services/test_execution_service.py"],
        ["app/api/routes/auth.py", "tests/security/test_session_and_attribution.py"],
        ["frontend/src/features/actions/ActionCard.tsx", "frontend/src/features/actions/ActionCard.test.tsx"],
        ["frontend/src/lib/format.ts", "frontend/src/test/fixtures.ts"],
        ["app/workflows/graph.py", "e2e/test_api_system.py"],
        ["docs/architecture.md", "README.md", ".github/workflows/ci.yml"],  # no production code
        ["tests/unit/domain/test_state_machine.py"],  # tests only
        [],
    ],
)
def test_changes_with_tests_or_without_production_code_pass(guard: ModuleType, changed: list[str]) -> None:
    result = guard.evaluate(changed)
    assert result.ok, result.message


@pytest.mark.parametrize(
    ("changed", "offender"),
    [
        (["app/services/execution_service.py"], "app/services/execution_service.py"),
        (
            ["app/db/migrations/versions/20261101_x_new.py", "docs/api.md"],
            "app/db/migrations/versions/20261101_x_new.py",
        ),
        (["frontend/src/features/inbox/InboxPage.tsx"], "frontend/src/features/inbox/InboxPage.tsx"),
        (["frontend/src/api/client.ts", "frontend/README.md"], "frontend/src/api/client.ts"),
    ],
)
def test_production_changes_without_tests_fail_and_name_the_files(
    guard: ModuleType, changed: list[str], offender: str
) -> None:
    result = guard.evaluate(changed)
    assert not result.ok
    assert offender in result.message
    assert offender in result.production


def test_non_code_files_inside_source_trees_are_not_production_code(guard: ModuleType) -> None:
    assert guard.evaluate(["app/demo/fixtures/demo_mailbox.json", "frontend/src/vite-env.d.ts"]).ok


def _run(changed: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=changed,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    )


def test_cli_exit_codes() -> None:
    assert _run("app/main.py\ntests/unit/api/test_main.py\n").returncode == 0
    failed = _run("app/main.py\n")
    assert failed.returncode == 1
    assert "app/main.py" in failed.stdout + failed.stderr


def test_cli_override_requires_a_reason_and_is_reported() -> None:
    allowed = _run("app/main.py\n", {"TDD_GUARD_OVERRIDE": "pure refactor: rename covered by existing tests"})
    assert allowed.returncode == 0
    assert "override" in (allowed.stdout + allowed.stderr).lower()
    assert _run("app/main.py\n", {"TDD_GUARD_OVERRIDE": "  "}).returncode == 1


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("Fix typo\n\nTDD-Exempt: pure rename covered by existing tests\n", "pure rename covered by existing tests"),
        ("tdd-exempt:   generated client regeneration  ", "generated client regeneration"),
        ("No exemption here", None),
        ("TDD-Exempt:", None),
        ("", None),
        ("TDD-Exempt: $(rm -rf /); `id`", "$(rm -rf /); `id`"),  # returned as data, never executed
    ],
)
def test_exemption_reason_is_read_from_the_pull_request_body(guard: ModuleType, body: str, reason: str | None) -> None:
    assert guard.exemption_reason(body) == reason


def test_cli_accepts_an_exemption_from_the_pull_request_body() -> None:
    allowed = _run("app/main.py\n", {"TDD_GUARD_PR_BODY": "Refactor\nTDD-Exempt: behaviour-preserving move"})
    assert allowed.returncode == 0
    assert "behaviour-preserving move" in allowed.stdout
