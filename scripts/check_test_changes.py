#!/usr/bin/env python3
"""TDD guard: a change to production code must include a change to tests.

Usage (CI, pull requests):
    git diff --name-only origin/main...HEAD | python scripts/check_test_changes.py

Exit 0 when no production code changed or tests changed too; exit 1 otherwise. For a genuine exception
(e.g. a pure rename already covered by existing tests) add a line `TDD-Exempt: <reason>` to the pull
request description (passed in as TDD_GUARD_PR_BODY) or set TDD_GUARD_OVERRIDE to a reason. The reason is
printed in the job log for reviewers. The PR body is treated strictly as data. Standard library only.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_FRONTEND_SRC = "frontend/src/"
_EXEMPT_RE = re.compile(r"^\s*tdd-exempt:[ \t]*(\S.*?)\s*$", re.IGNORECASE | re.MULTILINE)
_TEST_SUFFIXES = (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")


def is_test(path: str) -> bool:
    if path.startswith(("tests/", "e2e/")):
        return True
    if path.startswith(_FRONTEND_SRC):
        return path.endswith(_TEST_SUFFIXES) or path.startswith(f"{_FRONTEND_SRC}test/")
    return False


def is_production_code(path: str) -> bool:
    if is_test(path):
        return False
    suffix = PurePosixPath(path).suffix
    if path.startswith("app/"):
        return suffix == ".py"
    if path.startswith(_FRONTEND_SRC):
        return suffix in (".ts", ".tsx") and not path.endswith(".d.ts")
    return False


@dataclass
class Result:
    ok: bool
    production: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    message: str = ""


def evaluate(changed: list[str]) -> Result:
    files = [p.strip() for p in changed if p.strip()]
    production = [p for p in files if is_production_code(p)]
    tests = [p for p in files if is_test(p)]
    if not production:
        return Result(True, production, tests, "No production code changed.")
    if tests:
        return Result(
            True, production, tests, f"{len(production)} production file(s) changed with {len(tests)} test file(s)."
        )
    listing = "\n".join(f"  - {p}" for p in production)
    return Result(
        False,
        production,
        tests,
        "Production code changed without any test change (write the failing test first — "
        f"docs/TESTING_STRATEGY.md):\n{listing}",
    )


def exemption_reason(pr_body: str) -> str | None:
    match = _EXEMPT_RE.search(pr_body or "")
    return match.group(1) if match else None


def main() -> int:
    result = evaluate(sys.stdin.read().splitlines())
    if result.ok:
        print(f"TDD guard: OK — {result.message}")
        return 0
    override = os.environ.get("TDD_GUARD_OVERRIDE", "").strip() or exemption_reason(
        os.environ.get("TDD_GUARD_PR_BODY", "")
    )
    if override:
        print(f"TDD guard: override accepted — reason: {override}\n{result.message}")
        return 0
    print(f"TDD guard: FAILED — {result.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
