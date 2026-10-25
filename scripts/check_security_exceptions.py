#!/usr/bin/env python3
"""Scanner suppressions are never silent: each one cites a registered, unexpired exception.

Usage (CI and locally, from the repository root):
    git ls-files -z | python3 scripts/check_security_exceptions.py [--today YYYY-MM-DD]

The register is `security-exceptions.toml`. Every entry names the scanner and rule, scope, suppression
mechanism, owner, justification, compensating controls and an expiry date. In every tracked file
(documentation excepted) a suppression marker — an inline scanner comment such as bandit's or semgrep's,
a ruff flake8-bandit (`S…`) noqa, a scanner flag such as `--ignore-unfixed` or `--exclude-rule`,
`continue-on-error: true`, a gitleaks allowlist or a scanner ignore file entry — must cite an exception id
(`SEC-EX-NNN`) on the same line or on the comment line directly above it.

Exit 1 when a marker cites nothing or an unknown id, an entry is incomplete, expired or cited nowhere.
Exceptions expiring within 14 days are printed as warnings. Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import PurePosixPath

REGISTER = "security-exceptions.toml"
REQUIRED_FIELDS = (
    "id",
    "scanner",
    "rule",
    "scope",
    "mechanism",
    "owner",
    "justification",
    "compensating_controls",
    "expires",
)
WARN_WITHIN = timedelta(days=14)

_ID_RE = re.compile(r"^SEC-EX-\d{3}$")
_CITED_RE = re.compile(r"\bSEC-EX-\d{3,}\b")
_MARKERS = (
    ("bandit", re.compile(r"#.*\bnosec\b")),
    ("semgrep", re.compile(r"\bnosemgrep\b")),
    ("gitleaks", re.compile(r"\bgitleaks:allow\b")),
    ("trivy", re.compile(r"\btrivy:ignore\b")),
    ("ruff security rule", re.compile(r"#\s*noqa:[^#\n]*\bS\d{3}\b")),
    (
        "scanner flag",
        re.compile(
            r"--(?:ignore-vuln|ignore-unfixed|exclude-rule|skip-dirs|skip-files|ignorefile|exit-zero"
            r"|severity-level|confidence-level|baseline-commit)\b"
        ),
    ),
    ("continue-on-error", re.compile(r"^\s*continue-on-error:\s*true\b")),
)
_IGNORE_FILES = frozenset({".trivyignore", ".gitleaksignore", ".semgrepignore"})
# Files that describe the markers rather than use them.
_SELF = frozenset(
    {
        REGISTER,
        "scripts/check_security_exceptions.py",
        "tests/unit/tooling/test_check_security_exceptions.py",
        "tests/security/test_ci_pipeline_contract.py",  # lists the soft-fail patterns it forbids
    }
)


@dataclass(frozen=True)
class SecurityException:
    id: str
    scanner: str
    rule: str
    scope: str
    mechanism: str
    owner: str
    justification: str
    compensating_controls: str
    expires: date


@dataclass(frozen=True)
class Suppression:
    path: str
    line: int
    kind: str
    ids: tuple[str, ...]


@dataclass
class Report:
    entries: list[SecurityException] = field(default_factory=list)
    suppressions: list[Suppression] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_register(text: str) -> tuple[list[SecurityException], list[str]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [], [f"{REGISTER}: invalid TOML ({exc})"]
    entries: list[SecurityException] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(data.get("exception", []), start=1):
        ex_id = raw.get("id")
        label = ex_id if isinstance(ex_id, str) and ex_id else f"entry #{index}"
        problems = [f"{label}: missing or blank '{name}'" for name in REQUIRED_FIELDS if _blank(raw.get(name))]
        if isinstance(ex_id, str) and ex_id and not _ID_RE.match(ex_id):
            problems.append(f"{label}: id must look like SEC-EX-001")
        if not _blank(raw.get("expires")) and not isinstance(raw.get("expires"), date):
            problems.append(f"{label}: 'expires' must be a TOML date (YYYY-MM-DD)")
        if isinstance(ex_id, str) and ex_id in seen:
            problems.append(f"{label}: duplicate exception id")
        seen.add(str(ex_id))
        if problems:
            errors.extend(f"{REGISTER}: {p}" for p in problems)
            continue
        entries.append(SecurityException(**{name: raw[name] for name in REQUIRED_FIELDS}))
    return entries, errors


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def find_suppressions(path: str, text: str) -> list[Suppression]:
    if path in _SELF or PurePosixPath(path).suffix.lower() in {".md", ".markdown", ".rst"}:
        return []
    name = PurePosixPath(path).name
    lines = text.splitlines()
    if name == ".gitleaks.toml":
        return _gitleaks_allowlists(path, text, lines)
    found: list[Suppression] = []
    for number, line in enumerate(lines, start=1):
        if name in _IGNORE_FILES:
            kind = None if not line.strip() or line.lstrip().startswith("#") else f"{name} entry"
        else:
            kind = next((k for k, pattern in _MARKERS if pattern.search(line)), None)
        if kind is not None:
            found.append(Suppression(path, number, kind, _cited(lines, number)))
    return found


def _cited(lines: list[str], number: int) -> tuple[str, ...]:
    ids = _CITED_RE.findall(lines[number - 1])
    if not ids and number >= 2 and lines[number - 2].lstrip().startswith("#"):
        ids = _CITED_RE.findall(lines[number - 2])
    return tuple(dict.fromkeys(ids))


def _gitleaks_allowlists(path: str, text: str, lines: list[str]) -> list[Suppression]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return [Suppression(path, 1, "gitleaks config (invalid TOML)", ())]
    starts = [n for n, line in enumerate(lines, start=1) if line.strip() == "[[allowlists]]"]
    found = []
    for index, allowlist in enumerate(data.get("allowlists", [])):
        line = starts[index] if index < len(starts) else 1
        ids = tuple(dict.fromkeys(_CITED_RE.findall(str(allowlist.get("description", "")))))
        found.append(Suppression(path, line, "gitleaks allowlist", ids))
    for rule in data.get("rules", []):
        if "allowlists" in rule or "allowlist" in rule:
            found.append(Suppression(path, 1, f"gitleaks rule allowlist ({rule.get('id', '?')})", ()))
    return found


def check(files: Mapping[str, str], register_text: str, *, today: date) -> Report:
    entries, errors = parse_register(register_text)
    report = Report(entries=entries, errors=errors)
    known = {e.id: e for e in entries}
    cited: set[str] = set()
    for path in sorted(files):
        for suppression in find_suppressions(path, files[path]):
            report.suppressions.append(suppression)
            where = f"{suppression.path}:{suppression.line}"
            if not suppression.ids:
                report.errors.append(f"{where}: {suppression.kind} suppression cites no exception (SEC-EX-NNN)")
            for ex_id in suppression.ids:
                cited.add(ex_id)
                if ex_id not in known:
                    report.errors.append(f"{where}: cites {ex_id}, which is not in {REGISTER}")
    for ex in entries:
        if ex.expires < today:
            report.errors.append(f"{ex.id} expired on {ex.expires.isoformat()} — fix the finding or renew with review")
        elif ex.expires - today <= WARN_WITHIN:
            report.warnings.append(f"{ex.id} expires on {ex.expires.isoformat()}")
        if ex.id not in cited:
            report.errors.append(f"{ex.id} is not cited by any suppression — remove it from {REGISTER}")
    return report


def _read_files(listing: bytes) -> dict[str, str]:
    """Read the listed files (NUL- or newline-separated paths, as from `git ls-files -z`)."""
    separator = b"\0" if b"\0" in listing else b"\n"
    files: dict[str, str] = {}
    for raw in listing.split(separator):
        path = raw.decode("utf-8", "surrogateescape").strip()
        if not path:
            continue
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except FileNotFoundError, IsADirectoryError:
            continue  # deleted in the working tree / submodule
        if b"\0" in data[:8192]:
            continue  # binary
        files[path] = data.decode("utf-8", "replace")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--today", type=date.fromisoformat, default=date.today(), help="evaluation date")
    args = parser.parse_args(argv)
    files = _read_files(sys.stdin.buffer.read())
    report = check(files, files.get(REGISTER, ""), today=args.today)
    for ex in report.entries:
        print(f"{ex.id}  {ex.scanner}: {ex.rule}  (owner: {ex.owner}; expires {ex.expires.isoformat()})")
    print(f"{len(report.suppressions)} suppression(s), {len(report.entries)} registered exception(s).")
    for warning in report.warnings:
        print(f"WARNING: {warning}")
    for error in report.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
