"""Exhaustive model-based check of the create-at-most-once engine (`run_idempotent_create`).

A simulated Azure DevOps server records whether each create attempt was really applied. Every sequence of
attempt results (created / created-without-id / retry / ambiguous / permanent / adapter exception, each
ambiguous kind either applied or not) is combined with every lookup behaviour (absent / found / error at
any point) for several retry bounds. Invariants:

* a POST after the first one happens only when the previous attempt certainly did not apply ("retry")
  or a lookup confirmed the item is absent;
* at most one work item ever exists;
* the outcome is classified correctly: CREATED/EXISTING carry the item, FAILED means nothing exists,
  UNCERTAIN only follows an ambiguous attempt whose duplicate check failed.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import pytest

from app.core.errors import IntegrationError
from app.domain.enums import FailureCategory
from app.integrations.azure_devops.base import CreateOutcome, CreateOutcomeStatus, WorkItemRef
from app.integrations.azure_devops.idempotency import CreateAttempt, ref_summary, run_idempotent_create
from app.integrations.http import RetryPolicy

KINDS = (
    "created",
    "created_no_id",  # 2xx without a readable id: applied, but the adapter cannot prove it
    "retry",  # refused before processing (connect error, 408/429/503)
    "ambiguous_applied",  # e.g. read timeout after the server created the item
    "ambiguous_not_applied",
    "permanent",
    "raise_applied",  # unexpected adapter exception after the request was processed
    "raise_not_applied",
)
APPLIED = frozenset({"created", "created_no_id", "ambiguous_applied", "raise_applied"})
REF = WorkItemRef(id=4242, url="https://dev.azure.com/org/p/_workitems/edit/4242")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@dataclass
class Server:
    script: list[str]
    lookup_error_at: int | None = None  # index of the post-attempt lookup that fails (None: never)
    items: int = 0
    posts: int = 0
    lookups_after_post: int = 0
    safe_to_resend: bool = True  # nothing sent yet
    violations: list[str] = field(default_factory=list)
    saw_ambiguous: bool = False

    async def lookup(self) -> WorkItemRef | None:
        if self.posts:
            index = self.lookups_after_post
            self.lookups_after_post += 1
            if index == self.lookup_error_at:
                raise IntegrationError("query unavailable", category=FailureCategory.TRANSIENT, status=503)
        if self.items == 0:
            self.safe_to_resend = True  # confirmed absent
            return None
        return REF

    async def attempt(self) -> CreateAttempt:
        if self.posts and not self.safe_to_resend:
            self.violations.append(f"POST #{self.posts + 1} without confirming absence")
        kind = self.script[self.posts]
        self.posts += 1
        self.safe_to_resend = kind == "retry"
        if kind in APPLIED:
            self.items += 1
        if kind in (
            "ambiguous_applied",
            "ambiguous_not_applied",
            "raise_applied",
            "raise_not_applied",
            "created_no_id",
        ):
            self.saw_ambiguous = True
        match kind:
            case "created":
                return CreateAttempt(kind="created", work_item=REF, http_status=200, summary={"id": REF.id})
            case "created_no_id":
                return CreateAttempt(kind="created", work_item=None, http_status=200)
            case "retry":
                return CreateAttempt(kind="retry", http_status=503, category=FailureCategory.TRANSIENT, retry_after=1.5)
            case "ambiguous_applied" | "ambiguous_not_applied":
                return CreateAttempt(kind="ambiguous", category=FailureCategory.TIMEOUT, message="read timeout")
            case "permanent":
                return CreateAttempt(kind="permanent", http_status=400, category=FailureCategory.VALIDATION)
            case _:
                raise RuntimeError("adapter bug")


def check(server: Server, outcome: CreateOutcome, max_retries: int, sleeps: int) -> list[str]:
    problems = list(server.violations)
    status = outcome.status
    if server.items > 1:
        problems.append(f"duplicate: {server.items} items")
    if server.posts > max_retries + 1:
        problems.append(f"{server.posts} POSTs exceed the bound")
    if outcome.retry_count != server.posts - 1 or sleeps != outcome.retry_count:
        problems.append(f"retry_count={outcome.retry_count} posts={server.posts} sleeps={sleeps}")
    if status is CreateOutcomeStatus.CREATED and (server.items != 1 or outcome.work_item != REF):
        problems.append("CREATED without exactly one item")
    if status is CreateOutcomeStatus.FAILED and server.items != 0:
        problems.append("FAILED although the item exists")
    if status is CreateOutcomeStatus.UNCERTAIN:
        if not server.saw_ambiguous or server.lookup_error_at is None:
            problems.append("UNCERTAIN without an ambiguous attempt and a failed lookup")
        if outcome.error_category is not FailureCategory.AMBIGUOUS or outcome.work_item is not None:
            problems.append("UNCERTAIN must be ambiguous and carry no item")
    if status is CreateOutcomeStatus.EXISTING:
        problems.append("EXISTING after posting")
    if server.items == 1 and status not in (CreateOutcomeStatus.CREATED, CreateOutcomeStatus.UNCERTAIN):
        problems.append(f"item exists but outcome is {status}")
    if status is not CreateOutcomeStatus.CREATED and outcome.work_item is not None:
        problems.append("work_item set on a non-success outcome")
    return problems


async def test_every_attempt_and_lookup_sequence_upholds_the_invariants() -> None:
    failures: list[str] = []
    runs = 0
    statuses: set[CreateOutcomeStatus] = set()
    for max_retries in (0, 1, 2, 3):
        policy = RetryPolicy(max_retries=max_retries)
        for script in itertools.product(KINDS, repeat=max_retries + 1):
            for lookup_error_at in (None, *range(max_retries + 1)):
                server = Server(list(script), lookup_error_at)
                sleep = Recorder()
                outcome = await run_idempotent_create(
                    lookup=server.lookup, attempt=server.attempt, policy=policy, sleep=sleep
                )
                runs += 1
                statuses.add(outcome.status)
                problems = check(server, outcome, max_retries, len(sleep.calls))
                if problems:
                    failures.append(f"{max_retries=} {script=} {lookup_error_at=}: {problems}")
    assert failures == []
    assert runs > 20_000
    assert statuses == {CreateOutcomeStatus.CREATED, CreateOutcomeStatus.FAILED, CreateOutcomeStatus.UNCERTAIN}


async def test_initial_lookup_outcomes_never_post() -> None:
    posted: list[int] = []

    async def attempt() -> CreateAttempt:
        posted.append(1)
        return CreateAttempt(kind="created", work_item=REF)

    async def found() -> WorkItemRef | None:
        return REF

    existing = await run_idempotent_create(lookup=found, attempt=attempt, policy=RetryPolicy(), sleep=Recorder())
    assert existing.status is CreateOutcomeStatus.EXISTING
    assert existing.work_item == REF
    assert existing.response_summary == {"id": 4242, "rev": None, "url": REF.url, "found_by": "idempotency_tag"}

    async def failing() -> WorkItemRef | None:
        raise IntegrationError("denied", category=FailureCategory.PERMISSION, status=403)

    failed = await run_idempotent_create(lookup=failing, attempt=attempt, policy=RetryPolicy(), sleep=Recorder())
    assert failed.status is CreateOutcomeStatus.FAILED
    assert failed.error_category is FailureCategory.PERMISSION
    assert failed.http_status == 403
    assert failed.error_message == "Duplicate check failed before create; nothing was created. denied"
    assert posted == []

    async def broken() -> WorkItemRef | None:
        raise KeyError("bug")

    with pytest.raises(KeyError):  # non-integration errors before any write propagate unchanged
        await run_idempotent_create(lookup=broken, attempt=attempt, policy=RetryPolicy(), sleep=Recorder())
    assert posted == []


async def _single(*attempts: CreateAttempt, lookups: list[WorkItemRef | None] | None = None, max_retries: int = 3):
    queue = list(attempts)
    answers = list(lookups or [])
    sleep = Recorder()

    async def lookup() -> WorkItemRef | None:
        return answers.pop(0) if answers else None

    async def attempt() -> CreateAttempt:
        return queue.pop(0)

    outcome = await run_idempotent_create(
        lookup=lookup, attempt=attempt, policy=RetryPolicy(max_retries=max_retries), sleep=sleep
    )
    return outcome, sleep.calls


async def test_created_outcome_summary_and_status() -> None:
    outcome, _ = await _single(CreateAttempt(kind="created", work_item=REF, http_status=201))
    assert outcome.response_summary == ref_summary(REF)  # adapter gave no summary
    assert (outcome.http_status, outcome.retry_count) == (201, 0)


async def test_created_without_id_is_looked_up_instead_of_resent() -> None:
    outcome, sleeps = await _single(CreateAttempt(kind="created", http_status=200), lookups=[None, REF])
    assert outcome.status is CreateOutcomeStatus.CREATED
    assert outcome.response_summary["recovered_after_ambiguous_error"] is True
    assert sleeps == []


async def test_permanent_without_category_defaults_to_internal() -> None:
    outcome, _ = await _single(CreateAttempt(kind="permanent", message="nope"))
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is FailureCategory.INTERNAL
    assert outcome.error_message == "nope"


async def test_exhaustion_message_category_and_retry_after() -> None:
    retry = CreateAttempt(kind="retry", http_status=429, category=FailureCategory.RATE_LIMITED, retry_after=4.0)
    outcome, sleeps = await _single(retry, retry, max_retries=1)
    assert outcome.status is CreateOutcomeStatus.FAILED
    assert outcome.error_category is FailureCategory.RATE_LIMITED
    assert outcome.error_message == "Create failed (gave up after 2 attempt(s))"
    assert sleeps == [4.0]

    bare = CreateAttempt(kind="retry", message="refused")
    outcome, _ = await _single(bare, max_retries=0)
    assert outcome.error_category is FailureCategory.TRANSIENT
    assert outcome.error_message == "refused (gave up after 1 attempt(s))"


async def test_uncertain_message_asks_for_reconciliation() -> None:
    calls = {"n": 0}

    async def lookup() -> WorkItemRef | None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise TimeoutError("lookup hung")
        return None

    async def attempt() -> CreateAttempt:
        return CreateAttempt(kind="ambiguous", http_status=504)

    outcome = await run_idempotent_create(lookup=lookup, attempt=attempt, policy=RetryPolicy(), sleep=Recorder())
    assert outcome.status is CreateOutcomeStatus.UNCERTAIN
    assert outcome.http_status == 504
    assert outcome.error_message == (
        "The create request outcome is unknown; the duplicate check also failed. Reconcile before retrying."
    )
