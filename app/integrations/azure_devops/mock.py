"""In-memory **mock** Azure DevOps adapter for demo mode and tests. Makes no network calls.

Items live in a process-wide store keyed by organization (ids start at 1001; URLs use the `mock://`
scheme so they can never be mistaken for real work items). Creation runs through the same
create-at-most-once engine as the REST client, so idempotency semantics are identical.

`fail_mode` injects failures (each "once" mode applies to the first create attempt of that instance):

* `none`
* `transient_once` — first attempt is refused (HTTP 503 equivalent) → retried → CREATED, retry_count=1.
* `timeout_after_create_once` — first attempt creates the item but reports an ambiguous timeout → tag
  lookup finds it → CREATED (no duplicate).
* `lookup_unavailable_after_timeout` — as above, but the follow-up lookup fails → UNCERTAIN (the item does
  exist; a later reconcile with a fresh client finds it).
* `permanent_validation` — every attempt is rejected (HTTP 400 equivalent) → FAILED/validation, nothing created.
* `auth_error` — every call fails with an authorization error.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, get_args
from urllib.parse import quote

from app.core.errors import IntegrationError
from app.core.timeutil import utcnow
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
from app.integrations.azure_devops.client import build_patch_document, local_request_issues
from app.integrations.azure_devops.idempotency import CreateAttempt, ref_summary, run_idempotent_create
from app.integrations.azure_devops.mapping import FIELD_TAGS, FIELD_TITLE, IDEMPOTENCY_TAG_RE
from app.integrations.http import RetryPolicy, SleepFn, no_sleep

MockFailMode = Literal[
    "none",
    "transient_once",
    "timeout_after_create_once",
    "lookup_unavailable_after_timeout",
    "permanent_validation",
    "auth_error",
]
MOCK_FAIL_MODES: frozenset[str] = frozenset(get_args(MockFailMode))

MOCK_ORGANIZATION = "nuvexa-demo-org"
MOCK_PROJECTS: tuple[str, ...] = ("NUVEXA-Demo", "Customer-Portal")
MOCK_WORK_ITEM_TYPES: tuple[str, ...] = ("Bug", "Task", "User Story", "Issue", "Epic", "Feature")
_FIRST_ID = 1001


@dataclass
class MockWorkItem:
    id: int
    project: str
    work_item_type: str
    fields: dict[str, Any]
    tags: list[str]
    rev: int = 1
    state: str = "New"
    created_at: datetime = field(default_factory=utcnow)

    @property
    def url(self) -> str:
        return mock_work_item_url(self.project, self.id)

    def ref(self) -> WorkItemRef:
        title = self.fields.get(FIELD_TITLE)
        return WorkItemRef(
            id=self.id,
            url=self.url,
            rev=self.rev,
            project=self.project,
            work_item_type=self.work_item_type,
            title=title if isinstance(title, str) else None,
        )


def mock_work_item_url(project: str, work_item_id: int) -> str:
    return f"mock://azure-devops/{quote(project, safe='')}/workitems/{work_item_id}"


class _MockStore:
    def __init__(self) -> None:
        self.items: dict[int, MockWorkItem] = {}
        self.next_id = _FIRST_ID


_LOCK = threading.Lock()
_STORES: dict[str, _MockStore] = {}


def _store(organization: str) -> _MockStore:
    with _LOCK:
        return _STORES.setdefault(organization, _MockStore())


class MockAzureDevOpsClient:
    """`AzureDevOpsClient` backed by the in-memory store (labelled as a mock everywhere it surfaces)."""

    mode = "mock"

    def __init__(
        self,
        organization: str = MOCK_ORGANIZATION,
        fail_mode: MockFailMode = "none",
        *,
        retry_policy: RetryPolicy | None = None,
        sleep: SleepFn = no_sleep,
    ) -> None:
        if fail_mode not in MOCK_FAIL_MODES:
            raise ValueError(f"Unknown mock fail_mode: {fail_mode!r}")
        self.organization: str | None = organization
        self.fail_mode: MockFailMode = fail_mode
        self._org = organization
        self._policy = retry_policy or RetryPolicy(max_retries=3)
        self._sleep = sleep
        self._create_attempts = 0
        self._fail_next_lookup = False

    # -- test helpers ----------------------------------------------------------------------------
    @staticmethod
    def reset() -> None:
        """Clear every in-memory organization store (tests)."""
        with _LOCK:
            _STORES.clear()

    def items(self, project: str | None = None) -> list[MockWorkItem]:
        store = _store(self._org)
        with _LOCK:
            return [i for i in store.items.values() if project is None or i.project == project]

    @property
    def create_attempts(self) -> int:
        return self._create_attempts

    # -- protocol --------------------------------------------------------------------------------
    def _check_auth(self) -> None:
        if self.fail_mode == "auth_error":
            raise IntegrationError(
                "Mock Azure DevOps: authentication failed (simulated)",
                category=FailureCategory.AUTHORIZATION,
                status=401,
            )

    async def aclose(self) -> None:
        return None

    async def test_connection(self) -> ConnectionCheck:
        if self.fail_mode == "auth_error":
            return ConnectionCheck(
                ok=False,
                mode=self.mode,
                organization=self.organization,
                message="Mock Azure DevOps: authentication failed (simulated).",
                latency_ms=0,
            )
        return ConnectionCheck(
            ok=True,
            mode=self.mode,
            organization=self.organization,
            message="Mock Azure DevOps adapter (in-memory, no external calls).",
            latency_ms=0,
        )

    async def list_projects(self) -> list[ProjectInfo]:
        self._check_auth()
        return [
            ProjectInfo(id=f"mock-{name.lower()}", name=name, state="wellFormed", url=f"mock://azure-devops/{name}")
            for name in MOCK_PROJECTS
        ]

    async def list_work_item_types(self, project: str) -> list[WorkItemTypeInfo]:
        self._check_auth()
        if project not in MOCK_PROJECTS:
            raise IntegrationError(
                f"Mock Azure DevOps: project {project!r} not found", category=FailureCategory.NOT_FOUND
            )
        return [WorkItemTypeInfo(name=t, reference_name=f"Mock.{t.replace(' ', '')}") for t in MOCK_WORK_ITEM_TYPES]

    def _issues(self, request: WorkItemCreateRequest) -> list[FieldIssue]:
        issues = local_request_issues(request)
        if request.project not in MOCK_PROJECTS:
            issues.append(FieldIssue(field="System.TeamProject", message=f"Project {request.project!r} does not exist"))
        if request.work_item_type not in MOCK_WORK_ITEM_TYPES:
            issues.append(
                FieldIssue(
                    field="System.WorkItemType", message=f"Work item type {request.work_item_type!r} does not exist"
                )
            )
        return issues

    async def validate_work_item(self, request: WorkItemCreateRequest) -> WorkItemValidationResult:
        self._check_auth()
        issues = self._issues(request)
        return WorkItemValidationResult(valid=not issues, issues=issues)

    async def find_by_idempotency_tag(self, project: str, tag: str) -> WorkItemRef | None:
        self._check_auth()
        if not IDEMPOTENCY_TAG_RE.match(tag):
            raise IntegrationError("Invalid idempotency tag", category=FailureCategory.VALIDATION)
        if self._fail_next_lookup:
            self._fail_next_lookup = False
            raise IntegrationError(
                "Mock Azure DevOps: work item query unavailable (simulated)",
                category=FailureCategory.TRANSIENT,
                retryable=True,
                status=503,
            )
        matches = sorted(
            (i for i in self.items(project) if tag.lower() in {t.lower() for t in i.tags}), key=lambda i: i.id
        )
        return matches[0].ref() if matches else None

    async def create_work_item_idempotent(self, request: WorkItemCreateRequest) -> CreateOutcome:
        issues = self._issues(request)
        if issues:
            return CreateOutcome(
                status=CreateOutcomeStatus.FAILED,
                error_category=FailureCategory.VALIDATION,
                error_message="; ".join(i.message for i in issues),
            )
        return await run_idempotent_create(
            lookup=lambda: self.find_by_idempotency_tag(request.project, request.idempotency_tag),
            attempt=lambda: self._attempt(request),
            policy=self._policy,
            sleep=self._sleep,
        )

    # -- internals -------------------------------------------------------------------------------
    def _insert(self, request: WorkItemCreateRequest) -> MockWorkItem:
        patch = build_patch_document(request)
        fields = {op["path"].removeprefix("/fields/"): op["value"] for op in patch}
        tags = [t.strip() for t in str(fields.get(FIELD_TAGS, "")).split(";") if t.strip()]
        store = _store(self._org)
        with _LOCK:
            item = MockWorkItem(
                id=store.next_id,
                project=request.project,
                work_item_type=request.work_item_type,
                fields=fields,
                tags=tags,
            )
            store.items[item.id] = item
            store.next_id += 1
        return item

    async def _attempt(self, request: WorkItemCreateRequest) -> CreateAttempt:
        self._create_attempts += 1
        first = self._create_attempts == 1
        if self.fail_mode == "auth_error":
            return CreateAttempt(
                kind="permanent",
                http_status=401,
                category=FailureCategory.AUTHORIZATION,
                message="Mock Azure DevOps: authentication failed (simulated)",
            )
        if self.fail_mode == "permanent_validation":
            return CreateAttempt(
                kind="permanent",
                http_status=400,
                category=FailureCategory.VALIDATION,
                message="Mock Azure DevOps rejected the request: TF401320 rule error for field Title (simulated)",
            )
        if self.fail_mode == "transient_once" and first:
            return CreateAttempt(
                kind="retry",
                http_status=503,
                category=FailureCategory.TRANSIENT,
                message="Mock Azure DevOps temporarily unavailable (simulated HTTP 503)",
            )
        item = self._insert(request)
        if first and self.fail_mode in ("timeout_after_create_once", "lookup_unavailable_after_timeout"):
            self._fail_next_lookup = self.fail_mode == "lookup_unavailable_after_timeout"
            return CreateAttempt(
                kind="ambiguous",
                category=FailureCategory.TIMEOUT,
                message="Mock Azure DevOps create timed out after the request was sent (simulated)",
            )
        ref = item.ref()
        return CreateAttempt(
            kind="created", work_item=ref, http_status=200, summary=ref_summary(ref, state=item.state, mock=True)
        )
