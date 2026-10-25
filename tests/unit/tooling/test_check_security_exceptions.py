"""Scanner suppressions are never silent (scripts/check_security_exceptions.py).

Every inline suppression marker must cite a registered exception (owner, justification, compensating
controls, expiry), every registered exception must be in use, and an expired exception fails the build.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_security_exceptions.py"
TODAY = date(2026, 10, 25)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_security_exceptions", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    return _load()


def entry(ex_id: str = "SEC-EX-001", *, expires: str = "2026-12-31", **overrides: str) -> str:
    fields = {
        "id": ex_id,
        "scanner": "bandit",
        "rule": "B608",
        "scope": "app/integrations/azure_devops/client.py (WIQL query)",
        "mechanism": "inline marker",
        "owner": "Platform owner (Elarxsys)",
        "justification": "WIQL, not SQL; literals escaped and format-checked",
        "compensating_controls": "unit tests for wiql_literal and the tag format",
        **overrides,
    }
    body = "\n".join(f'{key} = "{value}"' for key, value in fields.items() if value != "<omit>")
    return f"[[exception]]\n{body}\nexpires = {expires}\n"


# Marker spellings (this file is listed in the checker's _SELF: it describes markers, it does not use them).
NOSEC = "no" + "sec"
NOSEMGREP = "no" + "semgrep"
GITLEAKS_ALLOW = "gitleaks" + ":allow"


# ------------------------------------------------------------------------------------- register


def test_a_complete_register_parses(guard: ModuleType) -> None:
    entries, errors = guard.parse_register(entry() + entry("SEC-EX-002"))
    assert errors == []
    assert [e.id for e in entries] == ["SEC-EX-001", "SEC-EX-002"]
    assert entries[0].expires == date(2026, 12, 31)
    assert entries[0].owner == "Platform owner (Elarxsys)"


@pytest.mark.parametrize(
    "field", ["scanner", "rule", "scope", "mechanism", "owner", "justification", "compensating_controls"]
)
def test_every_field_is_required(guard: ModuleType, field: str) -> None:
    _, errors = guard.parse_register(entry(**{field: "<omit>"}))
    assert any(field in e and "SEC-EX-001" in e for e in errors), errors


@pytest.mark.parametrize("field", ["owner", "justification", "compensating_controls"])
def test_blank_fields_are_rejected(guard: ModuleType, field: str) -> None:
    _, errors = guard.parse_register(entry(**{field: "   "}))
    assert any(field in e for e in errors), errors


def test_expiry_must_be_a_date(guard: ModuleType) -> None:
    _, errors = guard.parse_register(entry(expires='"next year"'))
    assert any("expires" in e for e in errors), errors
    _, missing = guard.parse_register(entry().replace("expires = 2026-12-31\n", ""))
    assert any("expires" in e for e in missing), missing


@pytest.mark.parametrize("bad_id", ["EX-1", "SEC-EX-1", "sec-ex-001", "SEC-EX-0001", ""])
def test_ids_have_a_fixed_format(guard: ModuleType, bad_id: str) -> None:
    _, errors = guard.parse_register(entry(bad_id))
    assert errors, bad_id


def test_duplicate_ids_are_rejected(guard: ModuleType) -> None:
    _, errors = guard.parse_register(entry() + entry())
    assert any("duplicate" in e.lower() and "SEC-EX-001" in e for e in errors), errors


def test_invalid_toml_is_reported_not_raised(guard: ModuleType) -> None:
    entries, errors = guard.parse_register("[[exception]\nid = ")
    assert entries == []
    assert errors


# ----------------------------------------------------------------------------------- suppressions


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("app/x.py", f'query = "SELECT " + x  # {NOSEC} B608'),
        ("app/x.py", f"claims = jwt.decode(t, options=o)  # {NOSEMGREP}: python.jwt.security.x"),
        ("app/x.py", "q = f'SELECT {x}'  # noqa: S608"),
        ("app/x.py", "subprocess.run(cmd)  # noqa: E501, S603"),
        ("tests/fixture.py", f'TOKEN = "abc"  # {GITLEAKS_ALLOW}'),
        ("Dockerfile", "# trivy" + ":ignore:AVD-DS-0002"),
        (".github/workflows/ci.yml", "        run: trivy image --ignore-unfixed x.tar"),
        (".github/workflows/ci.yml", "        run: pip-audit --ignore-vuln GHSA-xxxx -r r.txt"),
        (".github/workflows/ci.yml", "        run: semgrep scan --exclude-rule some.rule"),
        (".github/workflows/ci.yml", "        continue-on-error: true"),
        ("scripts/security-scan.sh", "trivy image --skip-dirs /app x"),
        ("scripts/security-scan.sh", "bandit -r app --exit-zero"),
        ("scripts/security-scan.sh", "bandit -r app --severity-level high"),
        ("scripts/security-scan.sh", "bandit -r app --confidence-level=high"),
        ("scripts/security-scan.sh", "semgrep scan --baseline-commit origin/main --error"),
    ],
)
def test_unregistered_suppressions_are_errors(guard: ModuleType, path: str, line: str) -> None:
    report = guard.check({path: f"first line\n{line}\nlast line\n"}, "", today=TODAY)
    assert report.errors, line
    assert any(f"{path}:2" in e for e in report.errors), report.errors


@pytest.mark.parametrize(
    "lines",
    [
        [f'query = "SELECT " + x  # noqa: S608  # {NOSEC} B608 -- SEC-EX-001'],
        ["# SEC-EX-001: WIQL literal (see security-exceptions.toml)", f'query = "SELECT " + x  # {NOSEC} B608'],
        ["          # SEC-EX-001", "          run: trivy image --ignore-unfixed x.tar"],
    ],
)
def test_suppressions_citing_a_registered_exception_pass(guard: ModuleType, lines: list[str]) -> None:
    report = guard.check({"app/x.py": "\n".join(lines) + "\n"}, entry(), today=TODAY)
    assert report.errors == []
    assert [s.ids for s in report.suppressions] == [("SEC-EX-001",)]


def test_a_comment_two_lines_above_does_not_count(guard: ModuleType) -> None:
    text = f"# SEC-EX-001\nx = 1\nquery = 'SELECT ' + x  # {NOSEC} B608\n"
    report = guard.check({"app/x.py": text}, entry(), today=TODAY)
    assert any("app/x.py:3" in e for e in report.errors), report.errors


def test_citing_an_unknown_exception_is_an_error(guard: ModuleType) -> None:
    report = guard.check({"app/x.py": f"q = 'S' + x  # {NOSEC} B608 -- SEC-EX-404\n"}, entry(), today=TODAY)
    assert any("SEC-EX-404" in e for e in report.errors), report.errors


def test_expired_exceptions_fail_even_when_cited(guard: ModuleType) -> None:
    files = {"app/x.py": f"q = 'S' + x  # {NOSEC} B608 -- SEC-EX-001\n"}
    assert guard.check(files, entry(expires="2026-10-25"), today=TODAY).errors == []  # last valid day
    expired = guard.check(files, entry(expires="2026-10-24"), today=TODAY)
    assert any("SEC-EX-001" in e and "expired" in e for e in expired.errors), expired.errors


def test_exceptions_close_to_expiry_are_warned_about(guard: ModuleType) -> None:
    files = {"app/x.py": f"q = 'S' + x  # {NOSEC} B608 -- SEC-EX-001\n"}
    report = guard.check(files, entry(expires="2026-11-02"), today=TODAY)
    assert report.errors == []
    assert any("SEC-EX-001" in w and "2026-11-02" in w for w in report.warnings), report.warnings


def test_registered_exceptions_that_nothing_cites_are_errors(guard: ModuleType) -> None:
    report = guard.check({"app/x.py": "x = 1\n"}, entry(), today=TODAY)
    assert any("SEC-EX-001" in e and "not cited" in e for e in report.errors), report.errors


def test_register_errors_are_reported_by_check(guard: ModuleType) -> None:
    report = guard.check({}, entry(owner="<omit>"), today=TODAY)
    assert any("owner" in e for e in report.errors), report.errors


def test_plain_mentions_and_documentation_are_not_suppressions(guard: ModuleType) -> None:
    files = {
        "app/x.py": "# Never add a bare suppression comment; cite an exception id instead.\nx = 1\n",
        "docs/CI_SECURITY_GATES.md": f"Use `# {NOSEC} B608 -- SEC-EX-123` and `--ignore-unfixed`.\n",
        "README.md": "--exclude-rule\n",
        "app/y.py": "minor_version = 'nosecond'\n",  # word boundary: not a marker
    }
    report = guard.check(files, "", today=TODAY)
    assert report.errors == []
    assert report.suppressions == []


# ------------------------------------------------------------------------------------ gitleaks


GITLEAKS_TOML = """
[extend]
useDefault = true

[[allowlists]]
description = "{description}"
targetRules = ["jwt"]
condition = "AND"
paths = ['''^tests/unit/x\\.py$''']
regexTarget = "line"
regexes = ['''^JWT = "eyJ.x.y"$''']
"""


def test_gitleaks_allowlists_must_cite_an_exception(guard: ModuleType) -> None:
    uncited = guard.check({".gitleaks.toml": GITLEAKS_TOML.format(description="synthetic fixture")}, "", today=TODAY)
    assert any(".gitleaks.toml" in e for e in uncited.errors), uncited.errors
    cited = guard.check(
        {".gitleaks.toml": GITLEAKS_TOML.format(description="SEC-EX-001: synthetic fixture")}, entry(), today=TODAY
    )
    assert cited.errors == []


def test_ignore_files_are_suppressions(guard: ModuleType) -> None:
    uncited = guard.check({".trivyignore": "CVE-2026-0001\n"}, "", today=TODAY)
    assert any(".trivyignore:1" in e for e in uncited.errors), uncited.errors
    cited = guard.check({".trivyignore": "# SEC-EX-001\nCVE-2026-0001\n"}, entry(), today=TODAY)
    assert cited.errors == []


# ------------------------------------------------------------------------------------------ CLI


def _run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    """Run the checker the way CI does: `git ls-files -z | python3 scripts/check_security_exceptions.py`."""
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=cwd, capture_output=True, check=True).stdout
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        input=listing.decode("utf-8", "surrogateescape"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_cli_fails_on_an_unregistered_marker_in_tracked_files(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, {"app/x.py": f"q = 'S' + x  # {NOSEC} B608\n", "security-exceptions.toml": ""})
    result = _run("--today", "2026-10-25", cwd=repo)
    assert result.returncode == 1
    assert "app/x.py:1" in result.stdout + result.stderr


def test_cli_lists_exceptions_and_passes_when_everything_is_registered(tmp_path: Path) -> None:
    repo = _git_repo(
        tmp_path,
        {"app/x.py": f"q = 'S' + x  # {NOSEC} B608 -- SEC-EX-001\n", "security-exceptions.toml": entry()},
    )
    result = _run("--today", "2026-10-25", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SEC-EX-001" in result.stdout
    assert "2026-12-31" in result.stdout


def test_cli_ignores_untracked_files(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, {"security-exceptions.toml": ""})
    (repo / "scratch.py").write_text(f"q = 'S' + x  # {NOSEC} B608\n")  # not git-added
    assert _run("--today", "2026-10-25", cwd=repo).returncode == 0


def test_newline_separated_listings_are_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _load()
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.bin").write_bytes(b"\0\1\2")
    monkeypatch.chdir(tmp_path)
    assert guard._read_files(b"a.py\nb.bin\nmissing.py\n\n") == {"a.py": "x = 1\n"}


def test_the_repository_register_is_valid_and_complete() -> None:
    result = _run("--today", "2026-10-25")
    assert result.returncode == 0, result.stdout + result.stderr
