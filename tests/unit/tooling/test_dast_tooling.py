"""The DAST harness (scripts/dast.sh, loadtest/zap/) keeps the coverage it claims.

Two harness bugs found while running the scans are pinned here, because both made the scans weaker without
failing them:

* ZAP's OpenAPI importer sent empty values for FastAPI's optional parameters (OpenAPI 3.1
  `anyOf: [X, {type: null}]`), so every list request was a 422 before it reached the database and the active
  scan never tested `q`, `status` and the other filters. `prepare_targets.scan_spec` must give every query
  parameter and every request body a valid example.
* The replacer rule that adds the scan's session skipped `/auth/logout` but not `/auth/logout/`; FastAPI
  redirects the latter to the former and ZAP followed it with the session cookie, so the active scans
  logged themselves out halfway. The rule must skip every variant of the two session-ending routes.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.main import create_app
from tests.conftest import make_settings

ROOT = Path(__file__).resolve().parents[3]
ACTION_ID = "6b8f124e-eaba-4f26-9f9e-a9a3d11d69e9"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "loadtest" / "zap" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def prepared_spec() -> dict[str, Any]:
    prepare = _load("prepare_targets")
    schema = create_app(make_settings("sqlite+aiosqlite:///:memory:")).openapi()
    ids = {
        "email_id": "3c91234f-55bb-482c-9edb-af87f48bd9fc",
        "run_id": "aef691f7-6cfb-4b4f-870f-08ae19687f4c",
        "action_id": ACTION_ID,
        "task_id": "0b3b1c1e-2f9a-4a51-9d1e-6a0f4c3d2e1f",
        "connection_id": "e053de6c-04f0-4b20-b8c9-c8bdc7be4259",
        "own_connection_id": "02a16e81-b8f6-4034-a939-47b5774d6cb0",
        "project": "NUVEXA-Demo",
    }
    action = {"id": ACTION_ID, "version": 2, "payload_hash": "a" * 64, "payload": {"title": "x"}}
    settings = {"version": 3, "settings": {}}
    spec: dict[str, Any] = prepare.scan_spec(
        schema, "http://frontend:8080", ids, prepare.body_examples(ids, action, settings)
    )
    return spec


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [(path, method, op) for path, ops in spec["paths"].items() for method, op in ops.items()]


def test_every_parameter_of_the_scan_spec_has_a_valid_example(prepared_spec: dict[str, Any]) -> None:
    missing = [
        f"{method.upper()} {path} {parameter['name']}"
        for path, method, op in _operations(prepared_spec)
        for parameter in op.get("parameters", [])
        if parameter.get("example") in (None, "", [])
    ]
    assert missing == []
    nullable = [
        f"{method.upper()} {path} {p['name']}"
        for path, method, op in _operations(prepared_spec)
        for p in op.get("parameters", [])
        if {"type": "null"} in p.get("schema", {}).get("anyOf", [])
    ]
    assert nullable == [], "ZAP sends '' for 'X or null' parameters"
    assert prepared_spec["servers"] == [{"url": "http://frontend:8080"}]


def test_every_request_body_of_the_scan_spec_has_an_example(prepared_spec: dict[str, Any]) -> None:
    bodies = [
        (f"{method.upper()} {path}", media)
        for path, method, op in _operations(prepared_spec)
        for media in op.get("requestBody", {}).get("content", {}).values()
    ]
    assert bodies, "the API has request bodies"
    assert [name for name, media in bodies if "example" not in media] == []
    approve = prepared_spec["paths"]["/api/v1/actions/{action_id}/approve"]["post"]
    example = approve["requestBody"]["content"]["application/json"]["example"]
    assert example["version"] == 2
    assert example["payload_hash"] == "a" * 64


def test_the_delete_connection_example_is_never_the_scanning_personas_own_mailbox(
    prepared_spec: dict[str, Any],
) -> None:
    delete = prepared_spec["paths"]["/api/v1/integrations/connections/{connection_id}"]["delete"]
    (parameter,) = delete["parameters"]
    assert parameter["example"] == "e053de6c-04f0-4b20-b8c9-c8bdc7be4259"  # ids["connection_id"], not own


@pytest.mark.parametrize(
    ("url", "authenticated"),
    [
        ("http://frontend:8080/api/v1/auth/logout", False),
        ("http://frontend:8080/api/v1/auth/logout/", False),  # 307 -> /auth/logout, followed by ZAP
        ("http://frontend:8080/api/v1/auth/logout?x=1", False),
        ("http://frontend:8080/api/v1/auth/logout/..;/", False),
        ("http://frontend:8080/api/v1/auth/demo-login", False),
        ("http://frontend:8080/api/v1/auth/demo-login/", False),
        ("http://frontend:8080/api/v1/auth/me", True),
        ("http://frontend:8080/api/v1/auth/login?redirect_to=/inbox", True),
        (f"http://frontend:8080/api/v1/actions/{ACTION_ID}/approve", True),
        ("http://frontend:8080/inbox", True),
    ],
)
def test_the_session_replacer_skips_every_session_ending_route(url: str, authenticated: bool) -> None:
    pattern = _load("nuvexa_hook").AUTHENTICATED_URLS
    assert bool(re.fullmatch(pattern, url)) is authenticated
