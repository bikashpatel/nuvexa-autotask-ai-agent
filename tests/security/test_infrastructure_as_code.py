"""Release gate: the Azure infrastructure as code keeps the documented security properties.

`infra/azure/main.json` is the ARM template that the pinned Bicep CLI generates from `infra/azure/main.bicep`
(`scripts/security-scan.sh config` fails when the committed file is out of date), so these assertions hold for what
would actually be deployed. Nothing here talks to Azure: the template is parsed as JSON and its ARM expressions are
evaluated offline by the small evaluator below, once per deployment stage and optional feature, with the values of
`infra/azure/main.bicepparam`.
"""

from __future__ import annotations

import itertools
import json
import re
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
AZURE = ROOT / "infra" / "azure"
TEMPLATE_PATH = AZURE / "main.json"
PARAMS_PATH = AZURE / "main.bicepparam"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"
SCAN_SCRIPT = ROOT / "scripts" / "security-scan.sh"

# The only role definitions the workload identities may hold (built-in role ids are public constants):
# AcrPull, Key Vault Secrets User, Cognitive Services OpenAI User.
ACR_PULL = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
VAULT_READER_ROLE = "4633458b-17de-408a-b874-0445c86b69e6"
COGNITIVE_SERVICES_OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
ROLE_SCOPES = {
    ACR_PULL: "Microsoft.ContainerRegistry/registries",
    VAULT_READER_ROLE: "Microsoft.KeyVault/vaults",
    COGNITIVE_SERVICES_OPENAI_USER: "Microsoft.CognitiveServices/accounts",
}

STAGES = ("foundation", "migrate", "apps")
APPS = {"frontend", "api", "worker", "otel-collector"}
BACKEND = ("api", "worker")
# Process-level variables read outside `Settings` (uvicorn, azure-identity, the OpenTelemetry SDK).
NON_SETTINGS_ENV = {"FORWARDED_ALLOW_IPS", "AZURE_CLIENT_ID", "OTEL_EXPORTER_OTLP_ENDPOINT"}
SECRET_ENV = re.compile(r"(SECRET|PASSWORD|TOKEN|_KEY|_KEYS|_PAT|DATABASE_URL|CONNECTION_STRING|HEADERS)$")


# --- A minimal, offline evaluator for the ARM template language ------------------------------------------------------


class Runtime(str):
    """A value only known during a real deployment (reference(), resourceId(), guid(), ...), kept as its text."""


_TOKEN = re.compile(r"\s*('(?:[^']|'')*'|-?\d+|[A-Za-z_]\w*|[().,\[\]])")


def _tokenize(text: str) -> Iterator[str]:
    text, pos = text.rstrip(), 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        assert match, f"cannot tokenize the ARM expression at {text[pos:]!r}"
        yield match.group(1)
        pos = match.end()


@dataclass
class _Parser:
    tokens: list[str]
    pos: int = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        token = self.tokens[self.pos]
        assert expected is None or token == expected, f"expected {expected!r}, got {token!r}"
        self.pos += 1
        return token

    def expression(self) -> tuple[Any, ...]:
        token = self.take()
        node: tuple[Any, ...]
        if token.startswith("'"):
            node = ("lit", token[1:-1].replace("''", "'"))
        elif re.fullmatch(r"-?\d+", token):
            node = ("lit", int(token))
        else:
            self.take("(")
            args = []
            while self.peek() != ")":
                args.append(self.expression())
                if self.peek() == ",":
                    self.take(",")
            self.take(")")
            node = ("call", token, args)
        while self.peek() in (".", "["):
            if self.take() == ".":
                node = ("prop", node, self.take())
            else:
                node = ("index", node, self.expression())
                self.take("]")
        return node


def _format(template: str, *args: Any) -> str:
    return re.sub(r"\{(\d+)\}", lambda m: str(args[int(m.group(1))]), template)


class Evaluator:
    """Evaluates one (possibly nested) template for a set of parameter values."""

    def __init__(self, template: dict[str, Any], values: dict[str, Any]) -> None:
        self.template = template
        self.values: dict[str, Any] = {}
        for name, spec in template.get("parameters", {}).items():
            if name in values:
                self.values[name] = values[name]
            elif "defaultValue" in spec:
                self.values[name] = self.value(spec["defaultValue"])
            else:
                self.values[name] = Runtime(f"<required:{name}>")
        self.variables: dict[str, Any] = {}
        self.lambda_scopes: list[dict[str, Any]] = []
        self.copy_index: int | None = None  # the current iteration of a resource loop

    def functions(self) -> dict[str, Callable[..., Any]]:
        return {
            "parameters": lambda name: self.values[name],
            "variables": self.variable,
            "concat": lambda *a: (
                [x for part in a for x in part] if a and isinstance(a[0], list) else "".join(map(str, a))
            ),
            "createArray": lambda *a: list(a),
            "createObject": lambda *a: dict(zip(a[::2], a[1::2], strict=True)),
            "if": lambda c, a, b: a if c else b,
            "equals": lambda a, b: a == b,
            "greater": lambda a, b: a > b,
            "greaterOrEquals": lambda a, b: a >= b,
            "less": lambda a, b: a < b,
            "lessOrEquals": lambda a, b: a <= b,
            "not": lambda a: not a,
            "and": lambda *a: all(a),
            "or": lambda *a: any(a),
            "empty": lambda a: not a,
            "contains": lambda a, b: b in a,
            "length": len,
            "join": lambda a, sep: sep.join(map(str, a)),
            "format": _format,
            "string": str,
            "toLower": lambda a: a.lower(),
            "toUpper": lambda a: a.upper(),
            "true": lambda: True,
            "false": lambda: False,
            "null": lambda: None,
            "json": json.loads,
            "union": lambda *a: {k: v for part in a for k, v in part.items()},
            "map": lambda items, function: [function(item) for item in items],
            "lambdaVariables": lambda name: self.lambda_scopes[-1][name],
            "copyIndex": self._copy_index,
            "environment": lambda: {
                "name": "AzureCloud",
                "suffixes": {"keyvaultDns": ".vault.azure.net", "acrLoginServer": ".azurecr.io"},
            },
            "resourceGroup": lambda: {"name": Runtime("<resource-group>"), "location": Runtime("<location>")},
            "subscription": lambda: {"subscriptionId": Runtime("<subscription-id>")},
            "tenant": lambda: {"tenantId": Runtime("<tenant-id>")},
            "resourceId": lambda *a: Runtime(f"resourceId({', '.join(map(str, a))})"),
            "subscriptionResourceId": lambda *a: Runtime(f"subscriptionResourceId({', '.join(map(str, a))})"),
            "extensionResourceId": lambda *a: Runtime(f"extensionResourceId({', '.join(map(str, a))})"),
            "reference": lambda *a: Runtime(f"reference({a[0]})"),
            "guid": lambda *a: Runtime(f"guid({', '.join(map(str, a))})"),
            "uniqueString": lambda *a: Runtime("<unique-string>"),
        }

    def _copy_index(self, *args: Any) -> int:
        assert self.copy_index is not None, "copyIndex() outside a resource loop (property loops are not supported)"
        assert not [a for a in args if isinstance(a, str)], "named copyIndex() (property loops) is not supported"
        return self.copy_index + sum(a for a in args if isinstance(a, int))

    def _in_scope(self, scope: dict[str, Any], body: tuple[Any, ...]) -> Any:
        self.lambda_scopes.append(scope)
        try:
            return self.evaluate(body)
        finally:
            self.lambda_scopes.pop()

    def variable(self, name: str) -> Any:
        if name not in self.variables:
            self.variables[name] = self.value(self.template["variables"][name])
        return self.variables[name]

    def value(self, raw: Any) -> Any:
        if isinstance(raw, dict):
            return {str(self.value(k)): self.value(v) for k, v in raw.items()}
        if isinstance(raw, list):
            return [self.value(v) for v in raw]
        if isinstance(raw, str) and raw.startswith("[[") and raw.endswith("]"):
            return raw[1:]
        if isinstance(raw, str) and raw.startswith("[") and raw.endswith("]"):
            parser = _Parser(list(_tokenize(raw[1:-1])))
            node = parser.expression()
            assert parser.peek() is None, raw
            return self.evaluate(node)
        return raw

    def evaluate(self, node: tuple[Any, ...]) -> Any:
        kind = node[0]
        if kind == "lit":
            return node[1]
        if kind == "call" and node[1] == "lambda":  # lambda('x', body): bind the names when called
            names, body = [self.evaluate(arg) for arg in node[2][:-1]], node[2][-1]
            return lambda *args: self._in_scope(dict(zip(names, args, strict=True)), body)
        if kind == "call":
            function = self.functions().get(node[1])
            assert function, f"ARM function {node[1]!r} is not supported by this test's evaluator"
            return function(*(self.evaluate(arg) for arg in node[2]))
        target = self.evaluate(node[1])
        if kind == "prop":
            return Runtime(f"{target}.{node[2]}") if isinstance(target, Runtime) else target[node[2]]
        return target[self.evaluate(node[2])]


@dataclass(frozen=True)
class Resource:
    type: str
    name: Any
    raw: dict[str, Any]  # as in the template
    body: dict[str, Any]  # evaluated

    @property
    def properties(self) -> dict[str, Any]:
        return self.body.get("properties", {})


def _deploy(template: dict[str, Any], values: dict[str, Any]) -> list[Resource]:
    """The resources a deployment with these parameter values would create, nested deployments included."""
    evaluator = Evaluator(template, values)
    resources = template.get("resources", [])
    deployed: list[Resource] = []
    for raw in resources.values() if isinstance(resources, dict) else resources:
        if raw.get("existing"):
            continue
        loop = raw.get("copy")
        for index in range(evaluator.value(loop["count"])) if loop else [None]:
            evaluator.copy_index = index
            if not evaluator.value(raw.get("condition", True)):
                continue
            unevaluated = {k: v for k, v in raw.items() if k not in ("condition", "copy")}
            if raw["type"] == "Microsoft.Resources/deployments":  # the nested template is evaluated on its own below
                unevaluated["properties"] = {k: v for k, v in raw["properties"].items() if k != "template"}
            body = evaluator.value(unevaluated)
            deployed.append(Resource(raw["type"], body.get("name"), raw, body))
            if raw["type"] == "Microsoft.Resources/deployments":
                inner = {k: v["value"] for k, v in body["properties"].get("parameters", {}).items()}
                deployed += _deploy(raw["properties"]["template"], inner)
        evaluator.copy_index = None
    return deployed


# --- Inputs ----------------------------------------------------------------------------------------------------------


@cache
def _template() -> dict[str, Any]:
    data = json.loads(TEMPLATE_PATH.read_text())
    assert isinstance(data, dict)
    return data


def _literal(text: str) -> Any:
    text = text.strip()
    if re.fullmatch(r"'(?:[^'\\]|\\.)*'", text):
        return text[1:-1].replace("\\'", "'")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text in ("true", "false"):
        return text == "true"
    if text.startswith("[") and text.endswith("]"):
        return [_literal(item) for item in re.findall(r"'(?:[^'\\]|\\.)*'|-?\d+|true|false", text)]
    env = re.fullmatch(r"readEnvironmentVariable\('(\w+)'(?:,\s*(.+))?\)", text)
    if env:
        return _literal(env.group(2)) if env.group(2) else Runtime(f"<env:{env.group(1)}>")
    raise AssertionError(f"main.bicepparam: keep parameter values simple literals, got {text!r}")


@cache
def _bicepparam() -> dict[str, Any]:
    values: dict[str, Any] = {}
    lines = PARAMS_PATH.read_text().splitlines()
    assert lines[0].strip() == "using 'main.bicep'", "main.bicepparam must target main.bicep"
    for line in lines[1:]:
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        match = re.fullmatch(r"param (\w+) = (.+)", line.strip())
        assert match, f"main.bicepparam: one `param name = value` per line, got {line!r}"
        values[match.group(1)] = _literal(match.group(2))
    return values


def _scenario(stage: str = "apps", **overrides: Any) -> list[Resource]:
    return _deploy(_template(), {**_bicepparam(), "stage": stage, **overrides})


SCENARIOS = [
    pytest.param({"stage": stage, "azureDevOpsMode": mode, "enableTracing": tracing}, id=f"{stage}-{mode}-{tracing}")
    for stage, mode, tracing in itertools.product(STAGES, ("pat", "entra_delegated"), (True, False))
]


def _expected_workloads(scenario: dict[str, Any]) -> set[str]:
    if scenario["stage"] != "apps":
        return set()
    return APPS if scenario["enableTracing"] else APPS - {"otel-collector"}


def _of_type(resources: list[Resource], type_: str) -> list[Resource]:
    return [r for r in resources if r.type.lower() == type_.lower()]


def _one(resources: list[Resource], type_: str) -> Resource:
    found = _of_type(resources, type_)
    assert len(found) == 1, f"expected exactly one {type_}, found {len(found)}"
    return found[0]


def _container(resource: Resource) -> dict[str, Any]:
    containers = resource.properties["template"]["containers"]
    assert len(containers) == 1, "one container per app/job"
    container: dict[str, Any] = containers[0]
    return container


def _apps(resources: list[Resource]) -> dict[str, Resource]:
    apps = {_container(app)["name"]: app for app in _of_type(resources, "Microsoft.App/containerApps")}
    assert len(apps) == len(_of_type(resources, "Microsoft.App/containerApps")), "container names must be unique"
    return apps


def _job(resources: list[Resource]) -> Resource:
    return _one(resources, "Microsoft.App/jobs")


def _env(resource: Resource) -> dict[str, dict[str, Any]]:
    env = _container(resource).get("env", [])
    names = [e["name"] for e in env]
    assert len(names) == len(set(names)), f"duplicate environment variables in {_container(resource)['name']}"
    return {e["name"]: e for e in env}


def _params_spec() -> dict[str, dict[str, Any]]:
    params: dict[str, dict[str, Any]] = _template()["parameters"]
    return params


def _documented_probes() -> list[dict[str, Any]]:
    blocks = re.findall(r"```yaml\n(.*?)```", DEPLOYMENT_DOC.read_text(), re.S)
    probes = [yaml.safe_load(block)["probes"] for block in blocks if "probes:" in block]
    assert len(probes) == 1, "docs/deployment.md documents the api probes in one YAML block"
    documented: list[dict[str, Any]] = probes[0]
    return documented


def _documented_key_vault_secrets() -> set[str]:
    section = DEPLOYMENT_DOC.read_text().split("### Secrets", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z0-9-]+)` \|", section, re.M))


# --- Generated template ----------------------------------------------------------------------------------------------


def test_template_is_compiled_from_bicep_by_the_pinned_cli() -> None:
    generator = _template()["metadata"]["_generator"]
    pinned = re.search(r'^BICEP_VERSION="([\d.]+)"$', SCAN_SCRIPT.read_text(), re.M)
    assert pinned, "scripts/security-scan.sh pins BICEP_VERSION"
    assert generator["name"] == "bicep"
    assert generator["version"].startswith(pinned.group(1) + "."), "regenerate main.json with the pinned Bicep CLI"
    assert (AZURE / "main.bicep").is_file()


def test_template_materializes_no_keys_or_credentials() -> None:
    text = TEMPLATE_PATH.read_text()
    # list*() would copy account keys or connection strings into the deployment; getSecret() a Key Vault value.
    assert not re.search(r"\blist[A-Z]\w*\(", text)
    assert "getSecret" not in text
    assert not _of_type(_scenario(), "Microsoft.KeyVault/vaults/secrets"), "secret values are set out of band"


# --- Ingress, probes and process types -------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_only_the_frontend_has_external_ingress(scenario: dict[str, Any]) -> None:
    apps = _apps(_deploy(_template(), {**_bicepparam(), **scenario}))
    assert set(apps) == _expected_workloads(scenario)
    external = {
        name for name, app in apps.items() if app.properties["configuration"].get("ingress", {}).get("external")
    }
    assert external <= {"frontend"}
    for app in apps.values():
        ingress = app.properties["configuration"].get("ingress")
        if ingress and ingress.get("allowInsecure"):
            assert ingress["external"] is False, "plain HTTP is accepted only on internal ingress"
    if "frontend" in apps:
        ingress = apps["frontend"].properties["configuration"]["ingress"]
        assert ingress["external"] is True
        assert ingress["targetPort"] == 8080
        assert ingress["allowInsecure"] is False, "HTTP must be redirected to HTTPS"


def test_api_is_internal_on_port_8000_with_the_documented_probes() -> None:
    api = _apps(_scenario())["api"]
    ingress = api.properties["configuration"]["ingress"]
    assert ingress["external"] is False
    assert ingress["targetPort"] == 8000
    assert _container(api)["probes"] == _documented_probes()
    liveness = next(p for p in _container(api)["probes"] if p["type"] == "Liveness")
    assert liveness["httpGet"]["path"] == "/api/v1/health/live", "a database blip must not restart healthy replicas"


def test_worker_has_no_ingress_and_outlives_the_shutdown_wait() -> None:
    worker = _apps(_scenario())["worker"]
    assert "ingress" not in worker.properties["configuration"]
    assert _container(worker)["command"] == ["nuvexa"]
    assert _container(worker)["args"] == ["worker"]
    assert "probes" not in _container(worker)
    # The worker waits up to 30 s for in-flight jobs on SIGTERM; the platform must wait longer.
    assert worker.properties["template"]["terminationGracePeriodSeconds"] > 30
    spec = _params_spec()["workerTerminationGracePeriodSeconds"]
    assert spec["minValue"] > 30, "no parameter value may undercut the worker's shutdown wait"


def test_otel_collector_is_internal_and_receives_otlp_over_http() -> None:
    collector = _apps(_scenario(enableTracing=True))["otel-collector"]
    ingress = collector.properties["configuration"]["ingress"]
    assert ingress["external"] is False
    assert ingress["targetPort"] == 4318
    assert "otel-collector" not in _apps(_scenario(enableTracing=False))
    for name in BACKEND:
        env = _env(_apps(_scenario(enableTracing=True))[name])
        assert env["OTEL_ENABLED"]["value"] == "true"
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == f"http://{collector.name}"


def test_migrate_job_is_manual_and_runs_the_same_backend_image_as_the_apps() -> None:
    resources = _scenario()
    job, apps = _job(resources), _apps(resources)
    config = job.properties["configuration"]
    assert config["triggerType"] == "Manual"
    assert config["manualTriggerConfig"] == {"parallelism": 1, "replicaCompletionCount": 1}
    assert config["replicaRetryLimit"] == 0, "a failed migration must fail the rollout, not retry silently"
    assert _container(job)["name"] == "migrate"
    assert _container(job)["command"] == ["nuvexa"]
    assert _container(job)["args"] == ["migrate"]
    images = {_container(job)["image"], _container(apps["api"])["image"], _container(apps["worker"])["image"]}
    assert len(images) == 1, "migrate, api and worker deploy the same backend image tag"
    assert images.pop().endswith(f"/nuvexa-backend:{_bicepparam()['backendImageTag']}")


def test_each_stage_deploys_only_what_the_rollout_order_allows() -> None:
    assert _params_spec()["stage"]["allowedValues"] == list(STAGES)
    assert "defaultValue" not in _params_spec()["stage"], "every deployment names its stage explicitly"
    foundation, migrate, apps = (_scenario(stage) for stage in STAGES)
    assert not _of_type(foundation, "Microsoft.App/jobs")
    assert not _of_type(foundation, "Microsoft.App/containerApps")
    assert _job(migrate)
    assert not _of_type(migrate, "Microsoft.App/containerApps"), "apps update only after the migration succeeded"
    assert set(_apps(apps)) == APPS
    # Within the apps stage: worker and api first, then the frontend.
    dependencies = " ".join(map(str, _apps(apps)["frontend"].raw.get("dependsOn", [])))
    for name in BACKEND:
        assert f"parameters('{_param_of(_apps(apps)[name].raw['name'])}')" in dependencies, f"frontend after {name}"


def _param_of(expression: str) -> str:
    match = re.fullmatch(r"\[parameters\('(\w+)'\)\]", expression)
    assert match, f"name the resource by a parameter: {expression}"
    return match.group(1)


# --- Identity and roles ----------------------------------------------------------------------------------------------


_IDENTITY_REF = re.compile(r"Microsoft\.ManagedIdentity/userAssignedIdentities, ([^,()\s]+)\)")
_SECRET_SCOPE = re.compile(r"resourceId\(Microsoft\.KeyVault/vaults/secrets, [^,()\s]+, ([\w-]+)\)")


def _identity_of(reference: Any) -> str:
    names = set(_IDENTITY_REF.findall(str(reference)))
    assert len(names) == 1, f"expected a reference to one managed identity: {reference}"
    return names.pop()


def _effective(name: str) -> Any:
    """The value of a parameter in a deployment with main.bicepparam (literal template defaults only)."""
    return _bicepparam().get(name, _params_spec()[name].get("defaultValue"))


def _workload_identity(resource: Resource) -> str:
    assert resource.body["identity"]["type"] == "UserAssigned"
    (assigned,) = resource.body["identity"]["userAssignedIdentities"]
    return _identity_of(assigned)


def _app_secret_names(scenario: dict[str, Any]) -> set[str]:
    apps = _apps(_deploy(_template(), {**_bicepparam(), **scenario, "stage": "apps"}))
    return {s["name"] for app in apps.values() for s in app.properties["configuration"].get("secrets", [])}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_each_identity_holds_only_the_documented_roles_on_the_resources_it_needs(scenario: dict[str, Any]) -> None:
    resources = _deploy(_template(), {**_bicepparam(), **scenario})
    apps_identity, migrate_identity = _effective("identityName"), _effective("migrateIdentityName")
    assert {str(i.name) for i in _of_type(resources, "Microsoft.ManagedIdentity/userAssignedIdentities")} == {
        apps_identity,
        migrate_identity,
    }
    held: dict[str, list[tuple[str, str]]] = {apps_identity: [], migrate_identity: []}
    for assignment in _of_type(resources, "Microsoft.Authorization/roleAssignments"):
        definition = str(assignment.properties["roleDefinitionId"])
        role = next((guid for guid in ROLE_SCOPES if guid in definition), None)
        assert role, f"unexpected role definition {definition}"
        assert assignment.properties["principalType"] == "ServicePrincipal"
        scope = str(assignment.body.get("scope", ""))
        # Scoped to the one resource it is needed on, never a resource group, subscription or the whole vault.
        assert ROLE_SCOPES[role] in scope, f"{role} must be scoped to a {ROLE_SCOPES[role]} resource, not {scope!r}"
        if role == VAULT_READER_ROLE:
            secret = _SECRET_SCOPE.fullmatch(scope)
            assert secret, f"Key Vault Secrets User is assigned per secret, never on the vault: {scope}"
            scope = secret.group(1)
        held[_identity_of(assignment.properties["principalId"])].append((role, scope))

    def secrets_of(identity: str) -> set[str]:
        return {scope for role, scope in held[identity] if role == VAULT_READER_ROLE}

    # Secret-level assignments need the secrets, which are set after the foundation stage.
    after_foundation = scenario["stage"] != "foundation"
    assert secrets_of(apps_identity) == (_app_secret_names(scenario) if after_foundation else set())
    assert secrets_of(migrate_identity) == ({"database-migrate-url"} if after_foundation else set())
    assert "database-migrate-url" not in secrets_of(apps_identity), "a compromised app must not read the owner URL"
    others = {identity: sorted(r for r, _ in roles if r != VAULT_READER_ROLE) for identity, roles in held.items()}
    assert others == {
        apps_identity: sorted([ACR_PULL, COGNITIVE_SERVICES_OPENAI_USER]),
        migrate_identity: [ACR_PULL],
    }


def test_the_identity_is_available_only_to_the_code_that_needs_it() -> None:
    resources = _scenario()
    expected = {"worker": "Main"}  # the worker calls Azure OpenAI with the identity; the rest only pull and resolve
    for resource in [*_apps(resources).values(), _job(resources)]:
        name = _container(resource)["name"]
        identity = _workload_identity(resource)
        assert identity == _effective("migrateIdentityName" if name == "migrate" else "identityName"), name
        settings = resource.properties["configuration"]["identitySettings"]
        assert [_identity_of(s["identity"]) for s in settings] == [identity]
        assert [s["lifecycle"] for s in settings] == [expected.get(name, "None")], name
        registries = resource.properties["configuration"]["registries"]
        assert registries
        assert all(set(r) == {"server", "identity"} for r in registries), "pull with the identity, no passwords"
        assert {_identity_of(r["identity"]) for r in registries} == {identity}


# --- Key Vault and secrets -------------------------------------------------------------------------------------------


def test_key_vault_uses_rbac_purge_protection_and_private_access() -> None:
    resources = _scenario()
    vault = _one(resources, "Microsoft.KeyVault/vaults").properties
    assert vault["enableRbacAuthorization"] is True
    assert vault["enablePurgeProtection"] is True
    assert vault["softDeleteRetentionInDays"] >= 7
    assert vault["publicNetworkAccess"] == "Disabled"
    assert vault["networkAcls"]["defaultAction"] == "Deny"
    assert vault["networkAcls"]["bypass"] == "None"
    for flag in ("enabledForDeployment", "enabledForTemplateDeployment", "enabledForDiskEncryption"):
        assert vault[flag] is False, flag
    assert "accessPolicies" not in vault or vault["accessPolicies"] == []
    endpoints = [
        conn["properties"]["groupIds"]
        for pe in _of_type(resources, "Microsoft.Network/privateEndpoints")
        for conn in pe.properties["privateLinkServiceConnections"]
    ]
    assert ["vault"] in endpoints, "the vault is reached through a private endpoint"


def test_the_template_defaults_are_the_secure_values_without_the_parameter_file() -> None:
    resources = _deploy(_template(), {"stage": "apps", "azureDevOpsMode": "pat"})  # every other value defaulted
    assert _one(resources, "Microsoft.KeyVault/vaults").properties["publicNetworkAccess"] == "Disabled"
    environment = _one(resources, "Microsoft.App/managedEnvironments").properties
    assert environment["peerTrafficConfiguration"]["encryption"]["enabled"] is True
    assert _apps(resources)["worker"].properties["template"]["terminationGracePeriodSeconds"] > 30
    password = _one(resources, "Microsoft.DBforPostgreSQL/flexibleServers").properties["administratorLoginPassword"]
    assert password is None, "an empty password parameter is omitted, never sent"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_app_secret_is_a_versionless_key_vault_reference(scenario: dict[str, Any]) -> None:
    resources = _deploy(_template(), {**_bicepparam(), **scenario})
    vault = _one(resources, "Microsoft.KeyVault/vaults")
    workloads = [*_apps(resources).values(), *_of_type(resources, "Microsoft.App/jobs")]
    assert set(_apps(resources)) == _expected_workloads(scenario)
    assert len(_of_type(resources, "Microsoft.App/jobs")) == (scenario["stage"] != "foundation")
    for resource in workloads:
        secrets = resource.properties["configuration"].get("secrets", [])
        for secret in secrets:
            assert set(secret) == {"name", "keyVaultUrl", "identity"}, f"no inline secret values: {secret}"
            assert secret["keyVaultUrl"] == f"https://{vault.name}.vault.azure.net/secrets/{secret['name']}"
            assert _identity_of(secret["identity"]) == _workload_identity(resource), "resolved with its own identity"
            assert secret["name"] in _documented_key_vault_secrets(), f"document {secret['name']} in deployment.md"
        declared = {s["name"] for s in secrets}
        for name, var in _env(resource).items():
            assert set(var) in ({"name", "value"}, {"name", "secretRef"}), var
            if "secretRef" in var:
                assert var["secretRef"] in declared, f"{name} references an undeclared secret"
            else:
                assert not SECRET_ENV.search(name), f"{name} must come from a Key Vault secret"
        assert declared == {v["secretRef"] for v in _env(resource).values() if "secretRef" in v}, "no unused secrets"
    for app in _of_type(resources, "Microsoft.App/containerApps"):
        if _container(app)["name"] == "frontend":
            assert not app.properties["configuration"].get("secrets"), "the frontend needs no secrets"


def test_backend_secrets_follow_the_integration_modes() -> None:
    for mode in ("pat", "entra_delegated"):
        apps = _apps(_scenario(azureDevOpsMode=mode))
        for name in BACKEND:
            env = _env(apps[name])
            for var, secret in {
                "DATABASE_URL": "database-url",
                "TOKEN_ENCRYPTION_KEYS": "token-encryption-keys",
                "AUDIT_HMAC_KEY": "audit-hmac-key",
                "ENTRA_CLIENT_SECRET": "entra-client-secret",
            }.items():
                assert env[var] == {"name": var, "secretRef": secret}
            assert ("AZURE_DEVOPS_PAT" in env) is (mode == "pat")
            assert "AZURE_OPENAI_API_KEY" not in env, "Azure OpenAI is reached with the managed identity"


def test_migrations_and_the_runtime_use_separate_database_roles_and_identities() -> None:
    resources = _scenario()
    job = _job(resources)
    # The schema owner (database-migrate-url) for the job only; the DML-only runtime role for api and worker.
    assert _env(job)["DATABASE_URL"] == {"name": "DATABASE_URL", "secretRef": "database-migrate-url"}
    assert {s["name"] for s in job.properties["configuration"]["secrets"]} == {"database-migrate-url"}
    role = _effective("databaseRuntimeRole")
    assert role == "nuvexa_app"
    assert _env(job)["DATABASE_RUNTIME_ROLE"] == {"name": "DATABASE_RUNTIME_ROLE", "value": role}, "the job grants it"
    for name, app in _apps(resources).items():
        declared = {s["name"] for s in app.properties["configuration"].get("secrets", [])}
        assert "database-migrate-url" not in declared, f"{name} must not hold the owner credential"
        assert _workload_identity(app) != _workload_identity(job), f"{name} must not share the job's identity"
    for name in BACKEND:
        env = _env(_apps(resources)[name])
        assert env["DATABASE_RUNTIME_ROLE"] == {"name": "DATABASE_RUNTIME_ROLE", "value": role}, "startup check"


# --- Application configuration ---------------------------------------------------------------------------------------


def test_backend_environment_uses_real_setting_names_and_production_values() -> None:
    settings = {name.upper() for name in Settings.model_fields}
    env_example = (ROOT / ".env.example").read_text()
    resources = _scenario()
    for resource in [*(_apps(resources)[name] for name in BACKEND), _job(resources)]:
        for name in _env(resource):
            assert name in settings or (name in NON_SETTINGS_ENV and name in env_example), f"unknown setting {name}"
    for name in BACKEND:
        env = {k: v.get("value") for k, v in _env(_apps(resources)[name]).items()}
        expected = {
            "APP_ENV": "production",
            "DEMO_MODE": "false",
            "EXPOSE_API_DOCS": "false",
            "RUN_EMBEDDED_WORKER": "false",
            "LOG_FORMAT": "json",
            "MAIL_MODE": "graph",
            "LLM_PROVIDER": "azure_openai",
            "AZURE_OPENAI_USE_ENTRA_ID": "true",
        }
        assert {k: env.get(k) for k in expected} == expected
        host = _bicepparam()["dashboardHost"]
        assert env["APP_BASE_URL"] == f"https://{host}"
        assert env["OAUTH_REDIRECT_URI"] == f"https://{host}/api/v1/auth/callback"
        assert _identity_of(env["AZURE_CLIENT_ID"]) == _effective("identityName")
    # uvicorn trusts X-Forwarded-For only from the environment's infrastructure subnet (the ingress proxies).
    subnets = _one(resources, "Microsoft.Network/virtualNetworks").properties["subnets"]
    infrastructure = next(
        s["properties"]["addressPrefix"]
        for s in subnets
        if [d["properties"]["serviceName"] for d in s["properties"].get("delegations", [])]
        == ["Microsoft.App/environments"]
    )
    assert _env(_apps(resources)["api"])["FORWARDED_ALLOW_IPS"]["value"] == infrastructure
    assert "FORWARDED_ALLOW_IPS" not in _env(_apps(resources)["worker"])


@pytest.mark.parametrize("max_replicas", [1, 2, 3, 10])
def test_api_rate_limits_are_shared_whenever_more_than_one_replica_can_run(max_replicas: int) -> None:
    api = _apps(_scenario(apiMaxReplicas=max_replicas))["api"]
    assert api.properties["template"]["scale"]["maxReplicas"] == max_replicas
    backend = _env(api)["RATE_LIMIT_BACKEND"]["value"]
    # Per-process counters would allow N x every limit; the shared counters need the same database and HMAC key.
    assert backend == ("database" if max_replicas > 1 else "memory")
    assert _env(api)["AUDIT_HMAC_KEY"] == {"name": "AUDIT_HMAC_KEY", "secretRef": "audit-hmac-key"}
    assert _env(api)["DATABASE_URL"] == {"name": "DATABASE_URL", "secretRef": "database-url"}
    assert _apps(_scenario())["api"].properties["template"]["scale"]["maxReplicas"] > 1, "documented scale rules"
    assert "RATE_LIMIT_BACKEND" not in _env(_apps(_scenario())["worker"]), "the worker does not rate-limit"


def test_frontend_proxies_to_the_internal_api_app_by_name() -> None:
    apps = _apps(_scenario())
    env = _env(apps["frontend"])
    assert env["API_UPSTREAM"] == {"name": "API_UPSTREAM", "value": f"http://{apps['api'].name}"}
    assert _container(apps["frontend"])["image"].endswith(f"/nuvexa-frontend:{_bicepparam()['frontendImageTag']}")


def test_the_dashboard_domain_is_served_over_sni_with_a_managed_certificate_once_bound() -> None:
    unbound = _apps(_scenario(dashboardManagedCertificateName=""))["frontend"]
    assert unbound.properties["configuration"]["ingress"]["customDomains"] == []
    bound = _apps(_scenario(dashboardManagedCertificateName="<certificate-name>"))["frontend"]
    (domain,) = bound.properties["configuration"]["ingress"]["customDomains"]
    assert domain["name"] == _bicepparam()["dashboardHost"]
    assert domain["bindingType"] == "SniEnabled"
    assert "managedCertificates" in str(domain["certificateId"])


# --- PostgreSQL, network and environment -----------------------------------------------------------------------------


def test_postgresql_is_private_tls_only_and_version_18() -> None:
    resources = _scenario()
    server = _one(resources, "Microsoft.DBforPostgreSQL/flexibleServers")
    props = server.properties
    assert props["version"] == "18"
    assert props["network"]["publicNetworkAccess"] == "Disabled"
    assert "virtualNetworks" in str(props["network"]["delegatedSubnetResourceId"])
    assert "privateDnsZones" in str(props["network"]["privateDnsZoneArmResourceId"])
    assert props["authConfig"] == {"activeDirectoryAuth": "Disabled", "passwordAuth": "Enabled"}
    assert not _of_type(resources, "Microsoft.DBforPostgreSQL/flexibleServers/firewallRules")
    configs = {
        str(c.name).rsplit("/", 1)[-1]: c.properties["value"]
        for c in _of_type(resources, "Microsoft.DBforPostgreSQL/flexibleServers/configurations")
    }
    assert configs["require_secure_transport"] == "ON"
    assert configs["ssl_min_protocol_version"] in ("TLSv1.2", "TLSv1.3")
    databases = _of_type(resources, "Microsoft.DBforPostgreSQL/flexibleServers/databases")
    assert [str(d.name).rsplit("/", 1)[-1] for d in databases] == ["nuvexa"]
    zones = [str(z.name) for z in _of_type(resources, "Microsoft.Network/privateDnsZones")]
    assert any(z.endswith(".postgres.database.azure.com") for z in zones)
    subnets = {
        s["name"]: s["properties"] for s in _one(resources, "Microsoft.Network/virtualNetworks").properties["subnets"]
    }
    delegations = {
        name: [d["properties"]["serviceName"] for d in props.get("delegations", [])] for name, props in subnets.items()
    }
    assert ["Microsoft.DBforPostgreSQL/flexibleServers"] in delegations.values()
    assert ["Microsoft.App/environments"] in delegations.values()


def test_container_apps_environment_is_vnet_integrated_and_logs_without_shared_keys() -> None:
    resources = _scenario()
    environment = _one(resources, "Microsoft.App/managedEnvironments").properties
    assert "virtualNetworks" in str(environment["vnetConfiguration"]["infrastructureSubnetId"])
    assert {"name": "Consumption", "workloadProfileType": "Consumption"} in environment["workloadProfiles"]
    assert environment["appLogsConfiguration"] == {"destination": "azure-monitor"}
    assert environment["peerTrafficConfiguration"]["encryption"]["enabled"] is True
    diagnostics = _one(resources, "Microsoft.Insights/diagnosticSettings")
    assert "managedEnvironments" in str(diagnostics.raw["scope"])
    assert "workspaces" in str(diagnostics.properties["workspaceId"])
    workspace = _one(resources, "Microsoft.OperationalInsights/workspaces").properties
    assert workspace["features"]["disableLocalAuth"] is True


# --- No secrets, identifiers or real domains in the sources ----------------------------------------------------------

GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
SECRET_SHAPES = re.compile(
    r"AccountKey=|InstrumentationKey=|SharedAccessSignature|[?&]sig=|-----BEGIN|eyJ[\w-]{10,}\."
    r"|postgres(?:ql)?(?:\+\w+)?://[^\s'\"<]+:[^\s'\"<]+@|\b[\w-]{43}=(?![\w=])|\b[a-z2-7]{52}\b"
)


def _sources() -> list[Path]:
    return sorted({*AZURE.rglob("*.bicep"), PARAMS_PATH, TEMPLATE_PATH, *AZURE.rglob("*.json"), *AZURE.rglob("*.yaml")})


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_hard_coded_secrets_ids_or_domains(path: Path) -> None:
    text = path.read_text()
    unexpected = set(GUID.findall(text)) - set(ROLE_SCOPES)
    assert not unexpected, f"no tenant, subscription or object ids in source: {unexpected}"
    assert not SECRET_SHAPES.search(text), "no keys, tokens or credentials in source"
    for email in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
        assert email.endswith(".example"), f"only placeholder addresses: {email}"
    hosts = re.findall(r"https?://([^\s/'\"{}<>\]$]+)", text)  # interpolated hosts (${...}, {0}) are parameters
    assert set(hosts) <= {"schema.management.azure.com", "aka.ms"}, f"no hard-coded hosts: {set(hosts)}"


def test_parameter_values_are_placeholders_not_real_identifiers() -> None:
    for name, value in _bicepparam().items():
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, str) or isinstance(item, Runtime):
                continue
            if re.search(r"<[^<>]+>", item) or re.fullmatch(r"[\d./]+", item):
                continue  # contains a <placeholder>, or is an address range
            assert "." not in item, f"{name}: use a <placeholder> for hosts"
            assert "@" not in item, f"{name}: use a <placeholder> for addresses"
            assert not GUID.search(item), f"{name}: use a <placeholder> for ids"


def test_trivy_ignore_entries_are_registered_exceptions_limited_to_one_check_and_path() -> None:
    register = tomllib.loads((ROOT / "security-exceptions.toml").read_text())
    expiry = {entry["id"]: entry["expires"] for entry in register["exception"]}
    ignore = yaml.safe_load((AZURE / ".trivyignore.yaml").read_text())
    assert set(ignore) == {"misconfigurations"}, "only misconfiguration findings may be ignored here"
    for entry in ignore["misconfigurations"]:
        cited = re.findall(r"SEC-EX-\d{3}", entry["statement"])
        assert len(cited) == 1, f"{entry['id']}: cite exactly one register entry"
        assert cited[0] in expiry, f"{cited[0]} is not in security-exceptions.toml"
        assert entry["expired_at"] == expiry[cited[0]], "the ignore expires with its register entry"
        assert entry["paths"] == ["infra/azure/main.json"], "one generated template, never a glob"
        assert re.fullmatch(r"[A-Z]+-\d{4}", entry["id"]), "one check id"


def test_secure_parameters_have_no_defaults_and_come_from_the_environment() -> None:
    secure = {name for name, spec in _params_spec().items() if spec["type"].lower() in ("securestring", "secureobject")}
    assert secure, "the PostgreSQL administrator password is a secure parameter"
    for name in secure:
        assert _params_spec()[name].get("defaultValue", "") == "", f"{name}: no default other than empty"
        line = next((ln for ln in PARAMS_PATH.read_text().splitlines() if ln.startswith(f"param {name} ")), None)
        if line is not None:
            assert "readEnvironmentVariable(" in line, f"{name} is read from the environment at deployment time"
    assert not _template().get("outputs") or all(
        spec["type"].lower() not in ("securestring", "secureobject") for spec in _template()["outputs"].values()
    )
