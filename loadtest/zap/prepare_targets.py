#!/usr/bin/env python3
"""Prepare an isolated demo stack for the ZAP scans in scripts/dast.sh (standard library, Python 3.12+).

1. Signs in every demo persona (POST /api/v1/auth/demo-login) and writes their session + CSRF cookies to
   <out>/sessions.json for the ZAP hook (loadtest/zap/nuvexa_hook.py). Synthetic demo sessions only.
2. Seeds synthetic demo data: the member syncs the demo mailbox and analyzes a few emails, so the scans
   reach real resources instead of only 404s.
3. Writes one OpenAPI description per scan persona (<out>/openapi.<persona>.json) that ZAP can exercise:
   * `servers` is the URL ZAP uses;
   * FastAPI's OpenAPI 3.1 "X or null" (`anyOf: [X, {type: null}]`) is reduced to X and enum references are
     inlined. ZAP's importer otherwise sends empty values (`?status=&category=`), every list request is a
     422 before it reaches the database, and the active scan never tests those parameters;
   * every path and query parameter and every request body has a valid example built from real data: the
     member's email, run and action (approver and admin see them tenant-wide, the viewer gets 404s), the
     action's current version and payload hash, the tenant settings. `connection_id` (DELETE) is always a
     connection the persona does not own, so an active scan cannot disconnect the scan's own mailbox; the
     sync body uses the persona's own mailbox.

Usage: prepare_targets.py --base-url http://127.0.0.1:38080 --zap-base-url http://frontend:8080 --out DIR
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PERSONAS = ("viewer", "member", "approver", "admin")
SCAN_PERSONAS = ("anonymous", *PERSONAS)
ANALYSIS_IN_FLIGHT = {"received", "normalized", "classified", "extracted", "validated", "action_proposed"}
EMAILS_TO_ANALYZE = 3
SESSION_COOKIE, CSRF_COOKIE = "nuvexa_session", "nuvexa_csrf"
MISSING_ID = "00000000-0000-4000-8000-000000000000"


class ApiClient:
    """A minimal browser-like JSON client: keeps the cookies and sends the CSRF header on unsafe methods."""

    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SystemExit(f"unsupported base URL: {base_url!r}")
        self.scheme, self.host, self.port = parts.scheme, parts.hostname, parts.port
        self.cookies: dict[str, str] = {}

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        connection_class = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        connection = connection_class(self.host, self.port, timeout=30)
        headers = {"Accept": "application/json", "User-Agent": "nuvexa-dast-prepare"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        if method not in ("GET", "HEAD") and CSRF_COOKIE in self.cookies:
            headers["X-CSRF-Token"] = self.cookies[CSRF_COOKIE]
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, f"/api/v1{path}", body=data, headers=headers)
            response = connection.getresponse()
            for header in response.headers.get_all("Set-Cookie") or []:
                jar: SimpleCookie = SimpleCookie()
                jar.load(header)
                self.cookies.update({name: morsel.value for name, morsel in jar.items()})
            raw = response.read()
        finally:
            connection.close()
        return response.status, (json.loads(raw) if raw else None)

    def ok(self, method: str, path: str, body: Any = None, expect: tuple[int, ...] = (200,)) -> Any:
        status, payload = self.call(method, path, body)
        if status not in expect:
            raise SystemExit(f"{method} {path} -> {status}: {payload}")
        return payload


def wait_until(predicate: Any, what: str, timeout: float = 90, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise SystemExit(f"timed out after {timeout:.0f}s waiting for {what}")


def login(base_url: str, persona: str) -> ApiClient:
    client = ApiClient(base_url)
    client.ok("POST", "/auth/demo-login", {"persona": persona})
    if SESSION_COOKIE not in client.cookies or CSRF_COOKIE not in client.cookies:
        raise SystemExit(f"demo-login as {persona} set no session/CSRF cookie")
    return client


def own_connection(client: ApiClient) -> str | None:
    connections = client.ok("GET", "/integrations")["microsoft"]["connections"]
    return str(connections[0]["id"]) if connections else None


def seed_member(member: ApiClient) -> dict[str, Any]:
    """Sync the member's demo mailbox, analyze a few emails, return the resources the scans use."""

    def emails() -> list[dict[str, Any]]:
        return list(member.ok("GET", "/emails?limit=100")["items"])

    if len(emails()) < EMAILS_TO_ANALYZE:
        member.ok("POST", "/emails/sync", {}, expect=(202,))
        wait_until(lambda: len(emails()) >= EMAILS_TO_ANALYZE, "the member's demo mailbox sync")
    runs = []
    for email in emails()[:EMAILS_TO_ANALYZE]:
        started = member.ok("POST", f"/emails/{email['id']}/analyze", expect=(200, 202))
        runs.append(started["workflow_run"]["id"])
    for run_id in runs:
        wait_until(
            lambda run_id=run_id: member.ok("GET", f"/workflows/{run_id}")["run"]["status"] not in ANALYSIS_IN_FLIGHT,
            f"analysis of run {run_id}",
        )
    actions = member.ok("GET", "/actions?limit=100")["items"]
    pending = [a for a in actions if a["status"] == "pending_approval"] or actions
    action = member.ok("GET", f"/actions/{pending[0]['id']}") if pending else None
    tasks = member.ok("GET", "/tasks?limit=100")["items"]
    return {
        "email_id": emails()[0]["id"],
        "run_id": runs[0],
        "action": action,
        "task_id": tasks[0]["id"] if tasks else None,
    }


def simplify(node: Any, enums: dict[str, dict[str, Any]]) -> Any:
    """`anyOf: [X, {type: null}]` -> X, and `$ref` to an enum component -> the enum itself (recursively)."""
    if isinstance(node, list):
        return [simplify(item, enums) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: simplify(value, enums) for key, value in node.items()}
    ref = node.get("$ref", "")
    if ref.startswith("#/components/schemas/") and ref.rsplit("/", 1)[1] in enums:
        rest = {k: v for k, v in node.items() if k != "$ref"}
        node = {**enums[ref.rsplit("/", 1)[1]], **rest}
    branches = node.get("anyOf")
    if isinstance(branches, list):
        concrete = [b for b in branches if b != {"type": "null"}]
        if len(concrete) == 1 and len(concrete) < len(branches):
            rest = {k: v for k, v in node.items() if k != "anyOf"}
            node = {**rest, **concrete[0]} if "$ref" not in concrete[0] else concrete[0]
    return node


def first_enum(schema: dict[str, Any]) -> Any:
    if schema.get("enum"):
        return schema["enum"][0]
    items = schema.get("items")
    if isinstance(items, dict) and items.get("enum"):
        return [items["enum"][0]]
    return None


def query_example(path: str, name: str, schema: dict[str, Any], ids: dict[str, Any]) -> Any:
    by_name = {
        "q": "invoice",
        "redirect_to": "/inbox",
        "limit": 25,
        "offset": 0,
        "event_type": "approval",
        "resource_type": "proposed_action",
        "resource_id": ids["action_id"],
        "email_id": ids["email_id"],
        "workflow_run_id": ids["run_id"],
        "action_id": ids["action_id"],
        "connected_account_id": ids["own_connection_id"],
    }
    if path.endswith("/actions") and name == "status":
        return ["pending_approval"]
    value = by_name.get(name)
    return value if value is not None else first_enum(schema)


def body_examples(ids: dict[str, Any], action: dict[str, Any] | None, settings: dict[str, Any]) -> dict[str, Any]:
    version = action["version"] if action else 1
    payload_hash = action["payload_hash"] if action else "0" * 64
    payload = action["payload"] if action else {}
    return {
        "DemoLoginRequest": {"persona": "member"},
        "SyncRequest": {"connected_account_id": ids["own_connection_id"]},
        "AdoValidateRequest": {"payload": payload, "server_side": True},
        "ActionUpdateRequest": {"expected_version": version, "payload": payload},
        "ApproveRequest": {"version": version, "payload_hash": payload_hash, "comment": "DAST"},
        "RejectRequest": {"reason": "DAST"},
        "CancelRequest": {"reason": "DAST"},
        "ExecuteRequest": {"version": version, "payload_hash": payload_hash},
        "TaskUpdateRequest": {"status": "done"},
        "SettingsUpdateRequest": {"expected_version": settings["version"], "settings": settings["settings"]},
    }


def scan_spec(schema: dict[str, Any], zap_base_url: str, ids: dict[str, Any], bodies: dict[str, Any]) -> dict[str, Any]:
    components = schema.get("components", {}).get("schemas", {})
    enums = {name: s for name, s in components.items() if "enum" in s}
    spec = simplify(schema, enums)
    spec["servers"] = [{"url": zap_base_url}]
    for path, operations in spec["paths"].items():
        for operation in operations.values():
            for parameter in operation.get("parameters", []):
                name = parameter["name"]
                if parameter.get("in") == "path":
                    # A resource the demo data has none of (e.g. no follow-up task yet): a well-formed id -> 404.
                    value = ids.get(name) or MISSING_ID
                else:
                    value = query_example(path, name, parameter.get("schema", {}), ids)
                if value is not None:
                    parameter["example"] = value
                    parameter.setdefault("schema", {})["example"] = value
            for media in operation.get("requestBody", {}).get("content", {}).values():
                ref = media.get("schema", {}).get("$ref", "")
                example = bodies.get(ref.rsplit("/", 1)[-1])
                if example is not None:
                    media["example"] = example
                    components_schema = spec["components"]["schemas"].get(ref.rsplit("/", 1)[-1])
                    if components_schema is not None:
                        components_schema["example"] = example
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="the stack as this machine reaches it")
    parser.add_argument("--zap-base-url", required=True, help="the stack as the ZAP container reaches it")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    clients = {persona: login(args.base_url, persona) for persona in PERSONAS}
    connections = {persona: own_connection(client) for persona, client in clients.items()}
    member = seed_member(clients["member"])
    projects = clients["member"].ok("GET", "/integrations/azure-devops/projects")["items"]
    settings = clients["admin"].ok("GET", "/settings")
    schema = clients["member"].ok("GET", "/openapi.json")

    for persona in SCAN_PERSONAS:
        own = connections.get(persona)
        foreign = connections["approver" if persona in ("anonymous", "viewer", "member") else "member"]
        ids = {
            "email_id": member["email_id"],
            "run_id": member["run_id"],
            "action_id": member["action"]["id"] if member["action"] else None,
            "task_id": member["task_id"],
            "connection_id": foreign,
            "own_connection_id": own,
            "project": projects[0]["name"] if projects else None,
        }
        bodies = body_examples(ids, member["action"], settings)
        spec = scan_spec(schema, args.zap_base_url.rstrip("/"), ids, bodies)
        (args.out / f"openapi.{persona}.json").write_text(json.dumps(spec, indent=1))

    sessions = {p: {"session": c.cookies[SESSION_COOKIE], "csrf": c.cookies[CSRF_COOKIE]} for p, c in clients.items()}
    (args.out / "sessions.json").write_text(json.dumps(sessions))
    action_id = member["action"]["id"] if member["action"] else None
    targets = {"member": {**member, "action_id": action_id}, "connections": connections, "projects": projects}
    (args.out / "targets.json").write_text(json.dumps(targets, indent=1))
    print(f"prepared {len(SCAN_PERSONAS)} scan specs and {len(sessions)} demo sessions in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
