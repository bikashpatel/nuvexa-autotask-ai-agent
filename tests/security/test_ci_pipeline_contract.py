"""Release gate: the CI pipeline keeps every required quality and security stage.

Removing a gate, letting a scanner soft-fail or lowering a coverage threshold fails this test, so such a
change needs a test change and therefore a reviewed, documented decision (docs/CI_SECURITY_GATES.md).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
# CI and local runs share one implementation of the scanner stages.
SCAN_SCRIPT = ROOT / "scripts" / "security-scan.sh"

# The jobs that must pass before a change may merge; `ci-gate` is the single required status check.
REQUIRED_JOBS = {
    "tdd-guard",
    "backend",
    "backend-sqlite",
    "frontend",
    "dependency-audit",
    "secret-scan",
    "sast",
    "containers",
    "pipeline-config",
}

# Pipeline stage -> (job, pattern every one of which must appear in that job's run scripts).
REQUIRED_STAGES: dict[str, tuple[str, list[str]]] = {
    "locked backend install": ("backend", [r"uv sync --locked"]),
    "locked frontend install": ("frontend", [r"npm ci\b"]),
    "backend lint and format": ("backend", [r"ruff check \.", r"ruff format --check \."]),
    "frontend lint and format": ("frontend", [r"npm run lint", r"npm run format:check"]),
    "type checks": ("backend", [r"mypy app"]),
    "frontend type check": ("frontend", [r"npm run typecheck"]),
    "unit tests": ("backend", [r"pytest -m unit\b"]),
    "integration tests (PostgreSQL)": ("backend", [r"pytest -m integration\b"]),
    "security regression tests": ("backend", [r"pytest -m security\b"]),
    "AI evaluation baseline": ("backend", [r"pytest -m evaluation\b"]),
    "backend coverage gate and report": ("backend", [r"coverage report", r"coverage xml"]),
    "SQLite (demo mode) suite with coverage gate": ("backend-sqlite", [r"pytest\b.*--cov=app"]),
    "frontend coverage thresholds": ("frontend", [r"npm run test:coverage"]),
    "migrations": ("backend", [r"alembic upgrade head"]),
    "python dependency audit": ("dependency-audit", [r"pip-audit\b.*--require-hashes"]),
    "npm dependency audit": ("dependency-audit", [r"npm audit --audit-level=high", r"npm audit signatures"]),
    "software bill of materials": ("dependency-audit", [r"cyclonedx", r"npm sbom"]),
    "secret scan of the full history": ("secret-scan", [r"gitleaks\b.* git\b"]),
    "SAST (semgrep)": ("sast", [r"semgrep scan\b.*--error"]),
    "SAST (bandit)": ("sast", [r"bandit -r app"]),
    "application build": ("frontend", [r"npm run build"]),
    "container image scan": ("containers", [r"trivy\b.* image\b.*--exit-code 1"]),
    "container SBOM": ("containers", [r"--format cyclonedx"]),
    "configuration scan": ("pipeline-config", [r"trivy\b.* config\b.*--exit-code 1"]),
    "infrastructure as code: lint and generated ARM template in sync": (
        "pipeline-config",
        [
            r"bicep lint\b",
            r"bicep build-params\b",
            r"bicep build infra/azure/main\.bicep",
            r"diff -u infra/azure/main\.json",
        ],
    ),
    "workflow lint": ("pipeline-config", [r"actionlint", r"zizmor"]),
    "scanner exceptions register": ("pipeline-config", [r"check_security_exceptions\.py"]),
    "opt-in system E2E": ("e2e", [r"scripts/e2e\.sh"]),
}

SCANNERS = re.compile(r"pip-audit|npm audit|gitleaks|semgrep|bandit|trivy|actionlint|zizmor|pytest|coverage|vitest")


def _ci() -> dict[str, Any]:
    data = yaml.safe_load(CI.read_text())
    assert isinstance(data, dict)
    return data


WORKFLOW = _ci()
JOBS: dict[str, Any] = WORKFLOW["jobs"]


def _stage_body(stage: str) -> str:
    match = re.search(rf"^stage_{stage}\(\) \{{\n(.*?)^\}}", SCAN_SCRIPT.read_text(), re.S | re.M)
    assert match, f"scripts/security-scan.sh has no stage_{stage}"
    return match.group(1)


def _runs(job: str) -> str:
    """The job's run scripts, with each `scripts/security-scan.sh <stage>` call expanded to that stage."""
    script = "\n".join(step.get("run", "") for step in JOBS[job].get("steps", []))
    stages = re.findall(r"scripts/security-scan\.sh (\w+)", script)
    return "\n".join([script, *(_stage_body(stage) for stage in stages)])


def test_required_jobs_exist() -> None:
    assert JOBS.keys() >= REQUIRED_JOBS, sorted(REQUIRED_JOBS - JOBS.keys())


@pytest.mark.parametrize("stage", sorted(REQUIRED_STAGES))
def test_every_required_stage_runs(stage: str) -> None:
    job, patterns = REQUIRED_STAGES[stage]
    assert job in JOBS, f"{stage}: job {job!r} is missing"
    script = _runs(job)
    missing = [p for p in patterns if not re.search(p, script)]
    assert not missing, f"{stage}: {job} does not run {missing}"


def test_the_gate_job_requires_every_required_job_and_fails_on_any_failure() -> None:
    gate = JOBS["ci-gate"]
    assert set(gate["needs"]) >= REQUIRED_JOBS
    assert gate.get("if") == "always()", "the gate must report even when a needed job fails or is skipped"
    script = _runs("ci-gate")
    assert "failure" in script
    assert "cancelled" in script


MASKED = re.compile(r"\|\|\s*(true|:|exit 0)\b|set \+e|--exit-zero")


def test_scanners_and_tests_never_soft_fail() -> None:
    for name, job in JOBS.items():
        assert not job.get("continue-on-error"), name
        for step in job.get("steps", []):
            run = step.get("run", "")
            if SCANNERS.search(run) or "security-scan.sh" in run:
                assert not step.get("continue-on-error"), f"{name}: {step.get('name')}"
                assert not MASKED.search(run), f"{name}: {step.get('name')} masks failures"
    assert WORKFLOW["defaults"]["run"]["shell"] == "bash", "bash -eo pipefail makes `scanner | tee` fail"


def test_the_scan_script_fails_fast_and_never_masks_a_scanner() -> None:
    script = SCAN_SCRIPT.read_text()
    assert "\nset -euo pipefail\n" in script
    stages = re.findall(r"^stage_(\w+)\(\) \{", script, re.M)
    assert set(stages) == {"deps", "sbom", "secrets", "sast", "images", "config"}
    for stage in stages:
        assert not MASKED.search(_stage_body(stage)), f"stage_{stage} masks failures"
    # `all` re-invokes the script per stage, so set -e still applies inside every stage.
    assert '"$0" "$stage" || failed+=' in script


def test_scanner_images_are_pinned_by_digest() -> None:
    images = re.findall(r'^(\w+_IMAGE)="([^"]+)"$', SCAN_SCRIPT.read_text(), re.M)
    assert {name for name, _ in images} == {
        "GITLEAKS_IMAGE",
        "SEMGREP_IMAGE",
        "TRIVY_IMAGE",
        "ACTIONLINT_IMAGE",
        "ZIZMOR_IMAGE",
        "BICEP_IMAGE",
    }
    for name, ref in images:
        assert re.search(r":[\w.-]+@sha256:[0-9a-f]{64}$", ref), f"{name} must be pinned as name:tag@sha256:digest"


def test_the_bicep_cli_is_pinned_and_verified_before_use() -> None:
    script = SCAN_SCRIPT.read_text()
    assert re.search(r'^BICEP_VERSION="\d+\.\d+\.\d+"$', script, re.M)
    for arch in ("X64", "ARM64"):
        assert re.search(rf'^BICEP_SHA256_LINUX_{arch}="[0-9a-f]{{64}}"$', script, re.M), arch
    body = re.search(r"^bicep\(\) \{\n(.*?)^\}", script, re.S | re.M)
    assert body, "scripts/security-scan.sh defines bicep()"
    # The downloaded binary is checked against the pinned sum before every run, and runs without network access.
    assert body.group(1).count('"$(sha256_of "$binary")" != "$expected"') == 2
    assert "--network none" in body.group(1)


def test_integration_tests_run_against_an_isolated_postgres_service() -> None:
    backend = JOBS["backend"]
    assert "postgres" in backend.get("services", {})
    assert backend["env"]["TEST_DATABASE_URL"].startswith("postgresql+psycopg://")
    assert "localhost" in backend["env"]["TEST_DATABASE_URL"]


def test_e2e_is_opt_in_and_uses_no_secrets() -> None:
    e2e = JOBS["e2e"]
    condition = e2e.get("if", "")
    assert "workflow_dispatch" in condition
    assert "inputs.e2e" in condition
    assert "secrets" not in yaml.safe_dump(e2e)


def test_test_reports_and_scan_results_are_published() -> None:
    uploads = {
        name
        for name, job in JOBS.items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/upload-artifact@") and step.get("if") == "always()"
    }
    assert {"backend", "backend-sqlite", "frontend", "dependency-audit", "secret-scan", "sast", "containers"} <= uploads
    assert "--junitxml" in _runs("backend")
    assert "junit" in _runs("frontend")


def test_coverage_thresholds_are_never_lowered() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["tool"]["coverage"]["report"]["fail_under"] >= 99
    assert pyproject["tool"]["coverage"]["run"]["branch"] is True
    vite = (ROOT / "frontend" / "vite.config.ts").read_text()
    floors = {"statements": 99, "lines": 100, "functions": 100, "branches": 98}
    for metric, floor in floors.items():
        match = re.search(rf"{metric}:\s*(\d+(?:\.\d+)?)", vite)
        assert match, metric
        assert float(match.group(1)) >= floor, f"frontend {metric} threshold lowered"


def test_the_secret_scan_covers_merge_commits_and_refuses_partial_history() -> None:
    script = SCAN_SCRIPT.read_text()
    # Without -m, git log -p shows no diff for a merge commit, so content written while resolving a merge
    # conflict would never be scanned.
    opts = re.search(r"^GITLEAKS_LOG_OPTS=\((.*)\)$", script, re.M)
    assert opts, "scripts/security-scan.sh defines GITLEAKS_LOG_OPTS"
    assert set(opts.group(1).split()) >= {"--full-history", "--all", "-m"}
    body = _stage_body("secrets")
    assert '--log-opts="${GITLEAKS_LOG_OPTS[*]}"' in body
    # The scanned count is checked against every commit git shows a diff for, not a count gitleaks cannot reach,
    # and a shallow clone is refused outright (its short history would also satisfy the count).
    assert 'expected="$(commits_with_diffs)"' in body
    assert '[ "$scanned" -ge "$expected" ]' in body
    assert "git rev-parse --is-shallow-repository" in body
