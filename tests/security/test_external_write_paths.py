"""Release gate: every path to an external system is reviewed, and there is exactly one write path.

The MVP may change exactly one thing outside its own database — create an Azure DevOps work item — and
only through `ExecutionService` after an authorized user approved the exact payload (CLAUDE.md
invariants). It reads mail with `Mail.Read` only and never sends, replies to, moves or deletes email.

These tests read the source (AST), so they fail as soon as a new module gains network capability, a new
non-GET request appears, or the work-item write gains a second caller. The failure is the prompt for a
security review: update the inventory below only together with that review (docs/CI_SECURITY_GATES.md).
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.integrations.microsoft_graph.base import GRAPH_MAIL_SCOPES

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

# Libraries that can open connections to other systems.
NETWORK_LIBRARIES = (
    "aiohttp",
    "azure",
    "boto3",
    "ftplib",
    "grpc",
    "http.client",
    "httpx",
    "imaplib",
    "msal",
    "openai",
    "opentelemetry.exporter",
    "poplib",
    "requests",
    "smtplib",
    "socket",
    "urllib.request",
    "urllib3",
    "websockets",
)

# Reviewed modules that may import a network library, and why.
APPROVED_NETWORK_MODULES = {
    "app/agents/openai_agent.py": "LLM analysis: sends the minimized email to the configured provider; read-only",
    "app/integrations/azure_devops/client.py": "Azure DevOps REST: metadata reads, WIQL, validateOnly, create",
    "app/integrations/azure_devops/factory.py": "builds the Azure DevOps HTTP client (no requests of its own)",
    "app/integrations/http.py": "shared retry helper for idempotent requests (no endpoints of its own)",
    "app/integrations/microsoft_graph/factory.py": "builds the Graph HTTP client (no requests of its own)",
    "app/integrations/microsoft_graph/id_token.py": "GET Microsoft signing keys (JWKS)",
    "app/integrations/microsoft_graph/mail_client.py": "GET-only Inbox delta query (Mail.Read)",
    "app/integrations/microsoft_graph/oauth.py": "MSAL authorization-code flow and token refresh",
    "app/integrations/microsoft_graph/token_store.py": "MSAL token cache (de)serialization only",
    "app/observability/tracing.py": "optional OTLP trace export (no email bodies in spans)",
    "app/services/auth_service.py": "MSAL token cache object for new connections only",
    "app/workers/runner.py": "socket.gethostname() for the worker id; no network I/O",
}

# Reviewed non-GET requests to external systems: (module, enclosing function) -> purpose.
APPROVED_NON_GET_REQUESTS = {
    ("app/integrations/azure_devops/client.py", "find_by_idempotency_tag"): "WIQL query (POST, read-only)",
    ("app/integrations/azure_devops/client.py", "validate_work_item"): "validateOnly=true create (writes nothing)",
    ("app/integrations/azure_devops/client.py", "_attempt_create"): "THE external write: create a work item",
}

# The one write entry point, and the only place allowed to call it.
WRITE_ENTRY_POINT = "create_work_item_idempotent"
APPROVED_WRITE_CALLERS = {("app/services/execution_service.py", "_dispatch")}

WRITE_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
HTTP_WRITE_METHODS = frozenset({"post", "put", "patch", "delete"})
# Graph permissions and endpoints that could change or send mail.
FORBIDDEN_GRAPH_FRAGMENTS = (
    "Mail.Send",
    "Mail.ReadWrite",
    "MailboxSettings.ReadWrite",
    "sendMail",
    "createReply",
    "createReplyAll",
    "createForward",
    "/replyAll",
    "/forward",
    "/move",
    "/copy",
)


def _modules() -> dict[str, ast.Module]:
    return {
        path.relative_to(ROOT).as_posix(): ast.parse(path.read_text(), filename=str(path))
        for path in sorted(APP.rglob("*.py"))
        if "migrations" not in path.parts
    }


MODULES = _modules()


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _is_network_library(name: str) -> bool:
    return any(name == lib or name.startswith(f"{lib}.") for lib in NETWORK_LIBRARIES)


def network_modules(modules: dict[str, ast.Module]) -> set[str]:
    return {path for path, tree in modules.items() if any(_is_network_library(n) for n in _imports(tree))}


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def _calls_in_body(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    # Body only: decorators such as @router.post(...) are routes, not outbound requests.
    return [n for statement in function.body for n in ast.walk(statement) if isinstance(n, ast.Call)]


def _is_database_delete(call: ast.Call) -> bool:
    # `session.delete(row)` and `CredentialStore(...).delete(...)` change the application's own database.
    receiver = ast.unparse(call.func.value) if isinstance(call.func, ast.Attribute) else ""
    return "session" in receiver or "Store(" in receiver


def non_get_requests(modules: dict[str, ast.Module]) -> set[tuple[str, str]]:
    """(module, function) pairs that send a non-GET request — in *any* module, so a client passed in
    from elsewhere (duck typing) is caught too."""
    found: set[tuple[str, str]] = set()
    for path, tree in modules.items():
        for function in _functions(tree):
            for call in _calls_in_body(function):
                method = call.func.attr if isinstance(call.func, ast.Attribute) else None
                literals = {a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)}
                http_write = method in HTTP_WRITE_METHODS and not (method == "delete" and _is_database_delete(call))
                if http_write or literals & WRITE_VERBS:
                    found.add((path, function.name))
    return found


def callers_of(modules: dict[str, ast.Module], name: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path, tree in modules.items():
        for function in _functions(tree):
            for call in _calls_in_body(function):
                if isinstance(call.func, ast.Attribute) and call.func.attr == name and function.name != name:
                    found.add((path, function.name))
    return found


def _string_constants(tree: ast.Module) -> list[str]:
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


# ----------------------------------------------------------------------------------------- gates


def test_only_reviewed_modules_can_reach_external_systems() -> None:
    actual = network_modules(MODULES)
    unreviewed = sorted(actual - APPROVED_NETWORK_MODULES.keys())
    assert not unreviewed, f"New network-capable modules need a security review: {unreviewed}"
    stale = sorted(APPROVED_NETWORK_MODULES.keys() - actual)
    assert not stale, f"Inventory lists modules that no longer use the network: {stale}"


def test_the_only_non_get_requests_are_the_reviewed_azure_devops_calls() -> None:
    actual = non_get_requests(MODULES)
    assert actual == APPROVED_NON_GET_REQUESTS.keys()


def test_the_work_item_write_has_exactly_one_caller_the_approval_gated_execution_service() -> None:
    assert callers_of(MODULES, WRITE_ENTRY_POINT) == APPROVED_WRITE_CALLERS


def test_graph_access_is_read_only_mail() -> None:
    assert GRAPH_MAIL_SCOPES == ("Mail.Read",)
    offending = sorted(
        (path, value)
        for path, tree in MODULES.items()
        for value in _string_constants(tree)
        if any(fragment in value for fragment in FORBIDDEN_GRAPH_FRAGMENTS)
    )
    assert not offending, f"Mail write/send capability needs explicit authorization: {offending}"


# ------------------------------------------------------------------------- the gates detect drift


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


def test_gate_detects_a_new_network_module() -> None:
    modules = {**MODULES, "app/services/notify.py": _parse("import smtplib\n")}
    assert "app/services/notify.py" in network_modules(modules) - APPROVED_NETWORK_MODULES.keys()


def test_gate_detects_a_new_write_request() -> None:
    source = (
        "import httpx\n"
        "async def close_ticket(client: httpx.AsyncClient) -> None:\n"
        "    await client.patch('https://dev.azure.com/x', json={})\n"
        "async def via_helper(client) -> None:\n"
        "    await request_idempotent(client, 'DELETE', 'https://graph.microsoft.com/v1.0/me/messages/1')\n"
    )
    modules = {"app/services/extra.py": _parse(source)}  # no network import needed: duck-typed client
    assert non_get_requests(modules) == {
        ("app/services/extra.py", "close_ticket"),
        ("app/services/extra.py", "via_helper"),
    }


def test_gate_ignores_route_decorators_get_requests_and_database_deletes() -> None:
    source = (
        "@router.post('/x')\n"
        "async def route() -> None:\n"
        "    await client.get('https://graph.microsoft.com/v1.0/me')\n"
        "    await request_idempotent(client, 'GET', 'https://graph.microsoft.com/v1.0/me')\n"
        "    await session.delete(row)\n"
        "    await CredentialStore(session, box).delete(account_id, tenant_id=t)\n"
    )
    assert non_get_requests({"app/api/x.py": _parse(source)}) == set()


def test_gate_detects_a_second_caller_of_the_write() -> None:
    source = "async def shortcut(client, request) -> None:\n    await client.create_work_item_idempotent(request)\n"
    modules = {**MODULES, "app/api/routes/shortcut.py": _parse(source)}
    assert ("app/api/routes/shortcut.py", "shortcut") in callers_of(modules, WRITE_ENTRY_POINT)
