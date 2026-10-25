"""Release gate: build and CI configuration stays pinned, least-privileged and free of secret exposure.

Covers the "secure pipeline practices" in docs/CI_SECURITY_GATES.md: third-party actions and base images
are pinned immutably, workflows default to read-only tokens, untrusted pull-request code never sees
secrets or gets untrusted text interpolated into scripts, dependency updates wait out a cooldown, npm
never runs install scripts, images run as non-root and the secret-scan allowlist stays precise.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
DOCKERFILES = [ROOT / "Dockerfile", ROOT / "frontend" / "Dockerfile"]
COOLDOWN_DAYS = 7

_SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), path
    return data


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` key as True
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return on or {}


def _steps(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(name, step) for name, job in workflow["jobs"].items() for step in job.get("steps", [])]


def test_there_are_workflows_to_check() -> None:
    assert WORKFLOWS


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_third_party_actions_are_pinned_to_full_commit_shas(path: Path) -> None:
    workflow = _load(path)
    uses = [step["uses"] for _, step in _steps(workflow) if "uses" in step]
    uses += [job["uses"] for job in workflow["jobs"].values() if "uses" in job]
    unpinned = [u for u in uses if not (u.startswith("./") or _SHA_PIN.match(u) or _DIGEST.search(u))]
    assert not unpinned, f"pin to a full commit SHA (tag in a comment): {unpinned}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_tokens_default_to_read_only(path: Path) -> None:
    workflow = _load(path)
    assert workflow.get("permissions") in ({}, {"contents": "read"}), "top-level permissions must be read-only"
    for name, job in workflow["jobs"].items():
        granted = job.get("permissions", {})
        assert isinstance(granted, dict), f"{name}: permissions must be an explicit mapping"
        writes = {scope: level for scope, level in granted.items() if level == "write"}
        assert not writes, f"{name} requests write scopes {writes}; document and review before adding"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_untrusted_pull_request_code_never_sees_secrets(path: Path) -> None:
    triggers = _triggers(_load(path))
    # These events run with secrets and a write token in the context of the base repository.
    assert not {"pull_request_target", "workflow_run"} & triggers.keys()
    assert "secrets." not in path.read_text(), "no repository secrets in CI; sandbox credentials need review"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_untrusted_event_text_is_never_interpolated_into_scripts(path: Path) -> None:
    untrusted = re.compile(r"\$\{\{\s*(github\.event\.|github\.head_ref|inputs\.)")
    for job, step in _steps(_load(path)):
        assert not untrusted.search(step.get("run", "")), f"{job}: pass untrusted values via env, not ${{{{ }}}}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkouts_do_not_persist_the_token(path: Path) -> None:
    checkouts = [s for _, s in _steps(_load(path)) if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts
    assert all(s.get("with", {}).get("persist-credentials") is False for s in checkouts)


def _from_lines(path: Path) -> list[str]:
    return [line.split()[1] for line in path.read_text().splitlines() if line.upper().startswith("FROM ")]


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_base_images_are_pinned_by_digest(path: Path) -> None:
    stages = {line.split()[-1] for line in path.read_text().splitlines() if re.match(r"(?i)^FROM .+ AS \S+$", line)}
    external = [image for image in _from_lines(path) if image not in stages]
    assert external
    unpinned = [image for image in external if not _DIGEST.search(image)]
    assert not unpinned, f"pin base images as name:tag@sha256:<digest> (Dependabot updates both): {unpinned}"


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_runtime_images_run_as_a_non_root_user(path: Path) -> None:
    final_stage = path.read_text().split("\nFROM ")[-1]
    users = re.findall(r"(?m)^USER\s+(\S+)", final_stage)
    assert users, "the runtime stage must set USER"
    assert users[-1].split(":")[0] not in {"root", "0"}


def test_compose_and_ci_service_images_are_pinned_by_digest() -> None:
    compose = _load(ROOT / "docker-compose.yml")
    third_party = [s["image"] for s in compose["services"].values() if "image" in s and "build" not in s]
    services = [
        svc["image"]
        for path in WORKFLOWS
        for job in _load(path)["jobs"].values()
        for svc in job.get("services", {}).values()
    ]
    assert third_party
    assert services
    unpinned = [image for image in third_party + services if not _DIGEST.search(image)]
    assert not unpinned, unpinned


def test_compose_publishes_ports_on_loopback_only() -> None:
    # Trivy does not parse Compose files, so the demo stack's exposure is pinned here.
    services = _load(ROOT / "docker-compose.yml")["services"]
    published = [str(port) for service in services.values() for port in service.get("ports", [])]
    assert published
    exposed = [port for port in published if not port.startswith("127.0.0.1:")]
    assert not exposed, f"publish on 127.0.0.1 only: {exposed}"


def test_dependency_updates_cover_every_ecosystem_and_wait_out_a_cooldown() -> None:
    updates = _load(ROOT / ".github" / "dependabot.yml")["updates"]
    covered = {(u["package-ecosystem"], u["directory"]) for u in updates}
    assert covered >= {
        ("uv", "/"),
        ("npm", "/frontend"),
        ("github-actions", "/"),
        ("docker", "/"),
        ("docker", "/frontend"),
        ("docker-compose", "/"),
    }
    short = [u["package-ecosystem"] for u in updates if u.get("cooldown", {}).get("default-days", 0) < COOLDOWN_DAYS]
    assert not short, f"cooldown.default-days must be >= {COOLDOWN_DAYS}: {short}"


def test_npm_never_runs_install_scripts_and_waits_out_new_releases() -> None:
    npmrc = ROOT / "frontend" / ".npmrc"
    assert npmrc.is_file()
    settings = dict(
        line.split("=", 1) for line in npmrc.read_text().splitlines() if "=" in line and not line.startswith("#")
    )
    assert settings.get("ignore-scripts") == "true"
    assert int(settings.get("min-release-age", "0")) >= COOLDOWN_DAYS
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text()
    copy_line = next(line for line in dockerfile.splitlines() if line.startswith("COPY package.json"))
    assert ".npmrc" in copy_line, "the image build must install with the same npm settings"


def test_installs_use_the_lockfiles() -> None:
    sources = [p.read_text() for p in [*WORKFLOWS, *DOCKERFILES, *(ROOT / "scripts").glob("*.sh")]]
    text = "\n".join(sources)
    assert not re.search(r"\bnpm (install|i)\b", text), "use npm ci"
    unlocked = [m.group(0) for m in re.finditer(r"uv sync[^\n]*", text) if not re.search(r"--locked|--frozen", m[0])]
    assert not unlocked, f"uv sync must use --locked: {unlocked}"


def test_secret_scan_allowlists_are_precise() -> None:
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert config["extend"]["useDefault"] is True  # extend the maintained rules, never replace them
    assert "rules" not in config, "custom rules/rule allowlists need review"
    allowlists = config.get("allowlists", [])
    assert allowlists
    for allowlist in allowlists:
        label = allowlist.get("description", "?")
        assert allowlist.get("condition") == "AND", f"{label}: path AND content must both match"
        assert allowlist.get("targetRules"), f"{label}: limit to specific rules"
        assert allowlist.get("regexTarget") == "line", f"{label}: match the exact line"
        assert allowlist.get("paths"), label
        assert allowlist.get("regexes"), label
        for pattern in allowlist["paths"] + allowlist["regexes"]:
            anchored = pattern.removeprefix("(?m)")
            assert anchored.startswith("^"), f"{label}: anchor {pattern!r}"
            assert anchored.endswith("$"), f"{label}: anchor {pattern!r}"
            assert ".*" not in pattern, f"{label}: no wildcards in {pattern!r}"
        pinned = allowlist.get("commits", [])
        assert all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in pinned), f"{label}: pin full commit SHAs"
        if not all(p.startswith("^tests/") for p in allowlist["paths"]):
            # Outside test fixtures only for specific historical commits whose text has since been changed.
            assert pinned, f"{label}: only test fixtures may be allowlisted (or specific historical commits)"


def test_commit_pinned_secret_allowlists_cover_history_only() -> None:
    # An allowlist pinned to old commits must not also hide the same text in the current tree.
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    pinned = [a for a in config.get("allowlists", []) if a.get("commits")]
    for allowlist in pinned:
        for path_pattern in allowlist["paths"]:
            path = ROOT / path_pattern.removeprefix("^").removesuffix("$").replace("\\.", ".")
            current = path.read_text() if path.is_file() else ""
            for pattern in allowlist["regexes"]:
                assert not re.search(pattern, current), f"{allowlist['description']}: still matches {path.name}"
