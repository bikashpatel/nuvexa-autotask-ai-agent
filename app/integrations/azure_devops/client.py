"""Azure DevOps REST 7.1 work-item client (PAT or Entra delegated bearer token).

URLs are built only from settings (base URL, organization, api-version) plus validated, percent-encoded
project / work-item-type names — never from model output or provider responses. Writes go through the
create-at-most-once engine in `idempotency.py`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Callable
from typing import Any, Self
from urllib.parse import quote, urlencode

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import IntegrationError, IntegrationNotConfigured
from app.domain.enums import FailureCategory
from app.integrations.azure_devops.base import (
    ConnectionCheck,
    CreateOutcome,
    CreateOutcomeStatus,
    FieldIssue,
    ProjectInfo,
    WorkItemCreateRequest,
    WorkItemRef,
    WorkItemTypeInfo,
    WorkItemValidationResult,
)
from app.integrations.azure_devops.idempotency import CreateAttempt, run_idempotent_create
from app.integrations.azure_devops.mapping import FIELD_TAGS, FIELD_TITLE, IDEMPOTENCY_TAG_RE
from app.integrations.http import (
    WRITE_SAFE_RETRY_STATUSES,
    RetryPolicy,
    SleepFn,
    TransportOutcome,
    classify_response,
    classify_transport_error,
    failure_category_of,
    parse_retry_after,
    request_idempotent,
    transport_failure_category,
)
from app.integrations.microsoft_graph.base import AZURE_DEVOPS_DELEGATED_SCOPES, AccessTokenProvider
from app.observability.logging import get_logger
from app.observability.tracing import start_span

log = get_logger(__name__)

_ORG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_API_VERSION_RE = re.compile(r"^\d+\.\d+(-preview(\.\d+)?)?$")
_FIELD_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_NAME = 256
_MAX_LOOKUP_IDS = 20
_MAX_PROJECT_PAGES = 100
_MAX_MESSAGE = 500
# Error bodies are untrusted and may echo request data, the Authorization header (PAT / bearer token) included.
# Only stable identifiers are propagated: the TFnnnnnn / VSnnnnnn code that prefixes `message`, the exception
# `typeKey` and a field reference name. The free-form text is never stored, returned or logged.
_ERROR_CODE_RE = re.compile(r"(?:TF|VS)\d{3,6}(?![0-9A-Za-z])")
_TYPE_KEY_RE = re.compile(r"[A-Z][A-Za-z0-9]{0,80}Exception")
_MAX_FIELD_REF = 128
_LOOKUP_FIELDS = (
    "System.Id",
    "System.Title",
    "System.State",
    "System.WorkItemType",
    "System.TeamProject",
    "System.Tags",
)
_JSON_PATCH = "application/json-patch+json"


# ---------------------------------------------------------------------------------------------
# Input validation and escaping
# ---------------------------------------------------------------------------------------------


def validate_name(value: str, *, what: str = "project") -> str:
    """Accept a project / work-item-type name only if it is safe to use as one URL path segment."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_NAME
        or value in {".", ".."}
        or any(ch in "/\\?#" for ch in value)
        or _CONTROL_RE.search(value)
    ):
        raise IntegrationError(f"Invalid Azure DevOps {what} name", category=FailureCategory.VALIDATION)
    return value


def _segment(value: str) -> str:
    return quote(value, safe="")


def wiql_literal(value: str) -> str:
    """WIQL string literal: single quotes are escaped by doubling them."""
    return "'" + value.replace("'", "''") + "'"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _clean_message(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(_CONTROL_RE.sub(" ", value).split())
    return text[:_MAX_MESSAGE] or None


def _error_codes(body: dict[str, Any] | None) -> list[str]:
    """The error code and exception type of an Azure DevOps error body (allow-listed shapes only)."""
    codes: list[str] = []
    message = (body or {}).get("message")
    if isinstance(message, str) and (code := _ERROR_CODE_RE.match(message.lstrip())):
        codes.append(code.group())
    type_key = (body or {}).get("typeKey")
    if isinstance(type_key, str) and _TYPE_KEY_RE.fullmatch(type_key):
        codes.append(type_key)
    return codes


def _error_field(body: dict[str, Any] | None) -> str | None:
    """The field reference name an Azure DevOps error body points at, when it is a well-formed one."""
    ref = _as_str(_as_dict(_as_dict(body).get("customProperties")).get("FieldReferenceName"))
    return ref if ref is not None and len(ref) <= _MAX_FIELD_REF and _FIELD_REF_RE.match(ref) else None


# ---------------------------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------------------------


class PatAuth:
    """Personal access token (service identity). Sent as HTTP Basic with an empty user name."""

    mode = "pat"

    def __init__(self, pat: SecretStr | str) -> None:
        secret = pat.get_secret_value() if isinstance(pat, SecretStr) else pat
        if not secret:
            raise IntegrationNotConfigured("AZURE_DEVOPS_PAT is not configured")
        self._header = "Basic " + base64.b64encode(f":{secret}".encode()).decode("ascii")

    async def authorization(self) -> str:
        return self._header

    def __repr__(self) -> str:
        return "PatAuth(<redacted>)"


class BearerTokenAuth:
    """Entra ID delegated token for Azure DevOps (`user_impersonation`), via an `AccessTokenProvider`.

    Tokens are memoised briefly: MSAL only returns access tokens with at least 5 minutes of validity left.
    """

    mode = "entra_delegated"

    def __init__(
        self,
        token_provider: AccessTokenProvider,
        *,
        scopes: tuple[str, ...] = AZURE_DEVOPS_DELEGATED_SCOPES,
        cache_seconds: float = 240.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = token_provider
        self._scopes = scopes
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    async def authorization(self) -> str:
        now = self._clock()
        if self._token is None or now >= self._expires_at:
            self._token = await self._provider.get_access_token(self._scopes)
            self._expires_at = now + self._cache_seconds
        return f"Bearer {self._token}"

    def __repr__(self) -> str:
        return "BearerTokenAuth(<redacted>)"


# ---------------------------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------------------------


class AzureDevOpsRestClient:
    """`AzureDevOpsClient` over the Azure DevOps Services REST API."""

    def __init__(
        self,
        settings: Settings,
        *,
        auth: PatAuth | BearerTokenAuth,
        http_client: httpx.AsyncClient | None = None,
        sleep: SleepFn = asyncio.sleep,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        organization = settings.azure_devops_organization
        if not organization or not _ORG_RE.match(organization):
            raise IntegrationNotConfigured("AZURE_DEVOPS_ORGANIZATION is missing or invalid")
        if not _API_VERSION_RE.match(settings.azure_devops_api_version):
            raise IntegrationNotConfigured("AZURE_DEVOPS_API_VERSION is invalid")
        base = settings.azure_devops_base_url.rstrip("/")
        if not base.startswith("https://"):
            raise IntegrationNotConfigured("AZURE_DEVOPS_BASE_URL must use https")
        self.mode: str = auth.mode
        self.organization: str | None = organization
        self._auth = auth
        self._base = base
        self._org_url = f"{base}/{_segment(organization)}"
        self._api_version = settings.azure_devops_api_version
        self._timeout = settings.azure_devops_timeout_seconds
        self._policy = retry_policy or RetryPolicy(max_retries=settings.azure_devops_max_retries)
        self._sleep = sleep
        self._client = http_client
        self._owns_client = http_client is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- plumbing --------------------------------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout), follow_redirects=False)
        return self._client

    def _url(self, *path: str, project: str | None = None, query: dict[str, str] | None = None) -> str:
        """`path` items are static API segments; only `project` comes from the caller (validated + encoded)."""
        parts = [self._org_url]
        if project is not None:
            parts.append(_segment(validate_name(project, what="project")))
        parts.extend(path)
        params = {**(query or {}), "api-version": self._api_version}
        return "/".join(parts) + "?" + urlencode(params, quote_via=quote, safe="$,")

    def _create_url(self, request: WorkItemCreateRequest, *, validate_only: bool) -> str:
        work_item_type = validate_name(request.work_item_type, what="work item type")
        query = {"validateOnly": "true"} if validate_only else None
        return self._url(
            "_apis", "wit", "workitems", f"${_segment(work_item_type)}", project=request.project, query=query
        )

    def html_url(self, project: str, work_item_id: int) -> str:
        return f"{self._org_url}/{_segment(project)}/_workitems/edit/{work_item_id}"

    async def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": await self._auth.authorization(),
            "Accept": "application/json",
            # Ask Azure DevOps for a 401 instead of a redirect / 203 sign-in page on bad credentials.
            "X-TFS-FedAuthRedirect": "Suppress",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    @staticmethod
    def _is_sign_in_response(response: httpx.Response) -> bool:
        status = response.status_code
        if status == 203 or 300 <= status < 400:
            return True
        return 200 <= status < 300 and "text/html" in response.headers.get("content-type", "").lower()

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any] | None:
        try:
            data = response.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _error(self, response: httpx.Response, *, operation: str) -> IntegrationError:
        status = response.status_code
        if self._is_sign_in_response(response):
            return IntegrationError(
                "Azure DevOps returned a sign-in page: the PAT or access token is invalid or expired",
                category=FailureCategory.AUTHORIZATION,
                status=status,
            )
        body = self._body(response)
        field = _error_field(body)
        identifiers = [*_error_codes(body), *([f"field {field}"] if field else [])]
        detail = f": {', '.join(identifiers)}" if identifiers else ""
        if status == 401:
            return IntegrationError(
                "Azure DevOps authentication failed: the PAT or access token is invalid or expired",
                category=FailureCategory.AUTHORIZATION,
                status=status,
            )
        if status == 403:
            return IntegrationError(
                f"The Azure DevOps identity is not permitted to {operation}{detail}",
                category=FailureCategory.PERMISSION,
                status=status,
            )
        if status == 404:
            return IntegrationError(
                f"Azure DevOps resource not found ({operation}){detail}",
                category=FailureCategory.NOT_FOUND,
                status=status,
            )
        if status == 400:
            return IntegrationError(
                f"Azure DevOps rejected the request ({operation}){detail}",
                category=FailureCategory.VALIDATION,
                status=status,
            )
        category, retryable = classify_response(status)
        return IntegrationError(
            f"Azure DevOps {operation} failed (HTTP {status}){detail}",
            category=category,
            retryable=retryable,
            status=status,
        )

    async def _send_idempotent(
        self, method: str, url: str, *, operation: str, json_body: Any = None, content: bytes | None = None
    ) -> dict[str, Any]:
        headers = await self._headers(content_type=_JSON_PATCH if content is not None else None)
        response = await request_idempotent(
            self._http(),
            method,
            url,
            policy=self._policy,
            sleep=self._sleep,
            headers=headers,
            json=json_body,
            content=content,
            timeout_seconds=self._timeout,
            operation=f"ado.{operation}",
        )
        if not 200 <= response.status_code < 300 or self._is_sign_in_response(response):
            raise self._error(response, operation=operation)
        body = self._body(response)
        if body is None:
            raise IntegrationError(
                f"Azure DevOps returned an unreadable response ({operation})",
                category=FailureCategory.TRANSIENT,
                retryable=True,
                status=response.status_code,
            )
        return body

    # -- read operations -------------------------------------------------------------------------
    async def test_connection(self) -> ConnectionCheck:
        started = time.monotonic()
        try:
            await self._send_idempotent(
                "GET", self._url("_apis", "projects", query={"$top": "1"}), operation="list projects"
            )
        except IntegrationError as exc:
            return ConnectionCheck(
                ok=False,
                mode=self.mode,
                organization=self.organization,
                message=_connection_message(exc),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        return ConnectionCheck(
            ok=True,
            mode=self.mode,
            organization=self.organization,
            message=f"Connected to Azure DevOps organization '{self.organization}'.",
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def list_projects(self) -> list[ProjectInfo]:
        projects: list[ProjectInfo] = []
        token: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_PROJECT_PAGES):
            query = {"$top": "100", **({"continuationToken": token} if token else {})}
            headers = await self._headers()
            response = await request_idempotent(
                self._http(),
                "GET",
                self._url("_apis", "projects", query=query),
                policy=self._policy,
                sleep=self._sleep,
                headers=headers,
                timeout_seconds=self._timeout,
                operation="ado.list_projects",
            )
            if not 200 <= response.status_code < 300 or self._is_sign_in_response(response):
                raise self._error(response, operation="list projects")
            body = self._body(response) or {}
            for item in body.get("value") or []:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("name"), str):
                    projects.append(
                        ProjectInfo(
                            id=item["id"],
                            name=item["name"],
                            state=_as_str(item.get("state")),
                            url=f"{self._org_url}/{_segment(item['name'])}",
                        )
                    )
            raw_token = response.headers.get("x-ms-continuationtoken") or body.get("continuationToken")
            token = str(raw_token) if raw_token not in (None, "") else None
            if token is None or token in seen:
                break
            seen.add(token)
        return projects

    async def list_work_item_types(self, project: str) -> list[WorkItemTypeInfo]:
        body = await self._send_idempotent(
            "GET", self._url("_apis", "wit", "workitemtypes", project=project), operation="list work item types"
        )
        types: list[WorkItemTypeInfo] = []
        for item in body.get("value") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                types.append(
                    WorkItemTypeInfo(
                        name=item["name"],
                        reference_name=_as_str(item.get("referenceName")),
                        description=_clean_message(item.get("description")),
                        is_disabled=item.get("isDisabled") is True,
                    )
                )
        return types

    async def find_by_idempotency_tag(self, project: str, tag: str) -> WorkItemRef | None:
        if not IDEMPOTENCY_TAG_RE.match(tag):
            raise IntegrationError("Invalid idempotency tag", category=FailureCategory.VALIDATION)
        validate_name(project, what="project")
        # WIQL, not SQL: both literals are escaped by wiql_literal() and the tag is format-checked above.
        query = (
            # SEC-EX-002 (security-exceptions.toml): the S608/B608 SQL-injection rules misfire on WIQL.
            "SELECT [System.Id] FROM WorkItems "  # noqa: S608  # nosec B608
            f"WHERE [System.TeamProject] = {wiql_literal(project)} AND [System.Tags] CONTAINS {wiql_literal(tag)} "
            "ORDER BY [System.Id] ASC"
        )
        result = await self._send_idempotent(
            "POST",
            self._url("_apis", "wit", "wiql", project=project, query={"$top": str(_MAX_LOOKUP_IDS)}),
            operation="query work items",
            json_body={"query": query},
        )
        found_ids = {_as_int(_as_dict(ref).get("id")) for ref in result.get("workItems") or []}
        ids = sorted(i for i in found_ids if i is not None)[:_MAX_LOOKUP_IDS]
        if not ids:
            return None
        detail = await self._send_idempotent(
            "GET",
            self._url(
                "_apis",
                "wit",
                "workitems",
                project=project,
                query={"ids": ",".join(str(i) for i in ids), "fields": ",".join(_LOOKUP_FIELDS), "errorPolicy": "omit"},
            ),
            operation="read work items",
        )
        candidates = [_as_dict(i) for i in detail.get("value") or []]  # omitted (deleted) items are null
        for item in sorted((i for i in candidates if _as_int(i.get("id")) is not None), key=lambda i: int(i["id"])):
            tags = _as_str(_as_dict(item.get("fields")).get(FIELD_TAGS))
            if tags is not None and tag not in {t.strip().lower() for t in tags.split(";")}:
                continue  # WIQL CONTAINS matched something else; require the exact tag
            return self._work_item_ref(item, project)
        return None

    def _work_item_ref(self, item: dict[str, Any], project: str) -> WorkItemRef:
        fields = _as_dict(item.get("fields"))
        item_id = int(item["id"])
        return WorkItemRef(
            id=item_id,
            url=self.html_url(project, item_id),
            rev=_as_int(item.get("rev")),
            project=_as_str(fields.get("System.TeamProject")) or project,
            work_item_type=_as_str(fields.get("System.WorkItemType")),
            title=_as_str(fields.get(FIELD_TITLE)),
        )

    # -- write operations ------------------------------------------------------------------------
    async def validate_work_item(self, request: WorkItemCreateRequest) -> WorkItemValidationResult:
        issues = local_request_issues(request)
        if issues:
            return WorkItemValidationResult(valid=False, issues=issues)
        content = json.dumps(build_patch_document(request)).encode("utf-8")
        headers = await self._headers(content_type=_JSON_PATCH)
        response = await request_idempotent(
            self._http(),
            "POST",
            self._create_url(request, validate_only=True),
            policy=self._policy,
            sleep=self._sleep,
            headers=headers,
            content=content,
            timeout_seconds=self._timeout,
            operation="ado.validate_work_item",
        )
        if 200 <= response.status_code < 300 and not self._is_sign_in_response(response):
            return WorkItemValidationResult(valid=True)
        if response.status_code == 400:
            body = self._body(response)
            codes = _error_codes(body)
            return WorkItemValidationResult(
                valid=False,
                issues=[
                    FieldIssue(
                        field=_error_field(body),
                        message="Azure DevOps rejected the work item" + (f" ({', '.join(codes)})" if codes else ""),
                    )
                ],
            )
        raise self._error(response, operation="validate work items")

    async def create_work_item_idempotent(self, request: WorkItemCreateRequest) -> CreateOutcome:
        issues = local_request_issues(request)
        if issues:
            return CreateOutcome(
                status=CreateOutcomeStatus.FAILED,
                error_category=FailureCategory.VALIDATION,
                error_message="; ".join(i.message for i in issues),
            )
        url = self._create_url(request, validate_only=False)
        content = json.dumps(build_patch_document(request)).encode("utf-8")
        with start_span(
            "azure_devops.create_work_item",
            {"ado.mode": self.mode, "ado.project": request.project, "ado.work_item_type": request.work_item_type},
        ) as span:
            outcome = await run_idempotent_create(
                lookup=lambda: self.find_by_idempotency_tag(request.project, request.idempotency_tag),
                attempt=lambda: self._attempt_create(url, content, request.project),
                policy=self._policy,
                sleep=self._sleep,
            )
            span.set_attribute("ado.outcome", outcome.status.value)
            span.set_attribute("ado.retry_count", outcome.retry_count)
            if outcome.http_status is not None:
                span.set_attribute("http.response.status_code", outcome.http_status)
            if outcome.error_category is not None:
                span.set_attribute("error.type", outcome.error_category.value)
        return outcome

    async def _attempt_create(self, url: str, content: bytes, project: str) -> CreateAttempt:
        try:
            headers = await self._headers(content_type=_JSON_PATCH)
        except IntegrationError as exc:  # token acquisition failed: nothing was sent
            return CreateAttempt(
                kind="retry" if exc.retryable else "permanent", category=failure_category_of(exc), message=exc.message
            )
        try:
            response = await self._http().post(url, content=content, headers=headers, timeout=self._timeout)
        except httpx.TransportError as exc:
            sent = classify_transport_error(exc)
            return CreateAttempt(
                kind="retry" if sent is TransportOutcome.NOT_SENT else "ambiguous",
                category=transport_failure_category(exc),
                message=f"Azure DevOps create request failed ({type(exc).__name__})",
            )
        status = response.status_code
        if 200 <= status < 300 and not self._is_sign_in_response(response):
            body = self._body(response) or {}
            if _as_int(body.get("id")) is None:
                return CreateAttempt(
                    kind="ambiguous",
                    http_status=status,
                    category=FailureCategory.AMBIGUOUS,
                    message="Azure DevOps returned an unreadable create response",
                )
            ref = self._work_item_ref(body, project)
            state = _as_str(_as_dict(body.get("fields")).get("System.State"))
            summary = {"id": ref.id, "rev": ref.rev, "state": state, "url": ref.url}
            return CreateAttempt(kind="created", work_item=ref, http_status=status, summary=summary)
        if status in WRITE_SAFE_RETRY_STATUSES:
            category, _ = classify_response(status)
            return CreateAttempt(
                kind="retry",
                http_status=status,
                category=category,
                message=f"Azure DevOps refused the create request (HTTP {status})",
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if status >= 500:
            return CreateAttempt(
                kind="ambiguous",
                http_status=status,
                category=FailureCategory.TRANSIENT,
                message=f"Azure DevOps returned HTTP {status} for the create request",
            )
        error = self._error(response, operation="create work items")
        return CreateAttempt(
            kind="permanent", http_status=status, category=failure_category_of(error), message=error.message
        )


# ---------------------------------------------------------------------------------------------
# Request helpers (shared with the mock adapter)
# ---------------------------------------------------------------------------------------------


def _merged_tags(request: WorkItemCreateRequest) -> list[str]:
    raw: list[str] = []
    existing = request.fields.get(FIELD_TAGS)
    if isinstance(existing, str):
        raw.extend(existing.split(";"))
    raw.extend(request.tags)
    raw.append(request.idempotency_tag)
    seen: set[str] = set()
    tags: list[str] = []
    for tag in (t.strip() for t in raw):
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags


def build_patch_document(request: WorkItemCreateRequest) -> list[dict[str, Any]]:
    """JSON Patch body for `POST …/workitems/${type}`; System.Tags always includes the idempotency tag."""
    ops: list[dict[str, Any]] = [
        {"op": "add", "path": f"/fields/{ref}", "value": value}
        for ref, value in request.fields.items()
        if ref != FIELD_TAGS
    ]
    ops.append({"op": "add", "path": f"/fields/{FIELD_TAGS}", "value": "; ".join(_merged_tags(request))})
    return ops


def local_request_issues(request: WorkItemCreateRequest) -> list[FieldIssue]:
    """Deterministic checks performed before any request leaves the process."""
    issues: list[FieldIssue] = []
    for what, value in (("project", request.project), ("work item type", request.work_item_type)):
        try:
            validate_name(value, what=what)
        except IntegrationError as exc:
            issues.append(FieldIssue(field=None, message=exc.message))
    if not IDEMPOTENCY_TAG_RE.match(request.idempotency_tag):
        issues.append(FieldIssue(field=FIELD_TAGS, message="Invalid idempotency tag"))
    title = request.fields.get(FIELD_TITLE)
    if not isinstance(title, str) or not title.strip():
        issues.append(FieldIssue(field=FIELD_TITLE, message="Title is required"))
    for ref, value in request.fields.items():
        if not _FIELD_REF_RE.match(ref):
            issues.append(FieldIssue(field=None, message="Invalid field reference name"))
        elif value is not None and not isinstance(value, str | int | float | bool):
            issues.append(FieldIssue(field=ref, message="Field values must be strings or numbers"))
    for tag in request.tags:
        if ";" in tag or _CONTROL_RE.search(tag):
            issues.append(FieldIssue(field=FIELD_TAGS, message="Tags must not contain ';' or control characters"))
    return issues


def _connection_message(exc: IntegrationError) -> str:
    """User-facing connection check message (never echoes credentials or raw provider responses)."""
    match exc.category:
        case FailureCategory.AUTHORIZATION:
            return (
                "Authentication failed: the PAT or access token is invalid, expired or not valid for this organization."
            )
        case FailureCategory.PERMISSION:
            return "Connected, but the identity is not permitted to list projects in this organization."
        case FailureCategory.NOT_FOUND:
            return "The Azure DevOps organization was not found."
        case FailureCategory.RATE_LIMITED:
            return "Azure DevOps is throttling requests; try again later."
        case _:
            return f"Azure DevOps could not be reached ({exc.category})."
