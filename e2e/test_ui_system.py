"""Browser end-to-end tests (Playwright, headless Chromium) against the deployed dashboard."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from e2e.conftest import ARTIFACTS, BASE_URL, ApiUser, find_action

playwright_api = pytest.importorskip("playwright.sync_api", reason="install the e2e group: uv sync --group e2e")
Browser = Any
Page = Any

Login = Callable[[str], ApiUser]
EXPECTED_CONSOLE_NOISE = re.compile(r"status of 401")  # session probe on the login page


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


def session_cookies(user: ApiUser) -> list[dict[str, Any]]:
    """Transfer an API session (HttpOnly session + CSRF cookies) into a browser context."""
    return [{"name": c.name, "value": c.value or "", "url": BASE_URL} for c in user.client.cookies.jar]


class UiSession:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.console_errors: list[str] = []
        page.on("console", self._on_console)
        page.on("pageerror", lambda e: self.console_errors.append(f"pageerror: {e}"))

    def _on_console(self, message: Any) -> None:
        if message.type == "error":
            self.console_errors.append(message.text)

    def login_as(self, user: ApiUser) -> None:
        self.page.context.add_cookies(session_cookies(user))

    def unexpected_errors(self) -> list[str]:
        return [e for e in self.console_errors if not EXPECTED_CONSOLE_NOISE.search(e)]


@pytest.fixture
def ui(browser: Browser, request: pytest.FixtureRequest) -> Iterator[UiSession]:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    session = UiSession(context.new_page())
    yield session
    failed = getattr(request.node, "rep_call", None)
    if failed is not None and failed.failed:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        session.page.screenshot(path=str(ARTIFACTS / f"{request.node.name}.png"), full_page=True)
    context.close()


MEMBER = "Sync your mailbox, analyze"


def test_login_page_offers_demo_personas_and_explains_entra(ui: UiSession) -> None:
    ui.page.goto(f"{BASE_URL}/login")
    expect = playwright_api.expect
    expect(ui.page.get_by_text("Sign in with Microsoft")).to_be_visible()
    for persona in ("Admin", "Approver", "Member", "Viewer"):
        expect(ui.page.get_by_text(persona, exact=True).first).to_be_visible()
    ui.page.get_by_text(MEMBER).first.click()  # the real persona login through the UI
    ui.page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    expect(ui.page.get_by_text("Welcome back").first).to_be_visible()
    assert ui.unexpected_errors() == []


def test_every_page_renders_without_errors(ui: UiSession, login: Login) -> None:
    ui.login_as(login("member"))
    for path, heading in [
        ("/", "Welcome back"),
        ("/inbox", "Inbox"),
        ("/approvals", "Approvals"),
        ("/workflows", "Workflows"),
        ("/tasks", "Tasks"),
        ("/integrations", "Integrations"),
        ("/settings", "Settings"),
        ("/audit", "Audit"),
    ]:
        ui.page.goto(f"{BASE_URL}{path}")
        ui.page.wait_for_load_state("networkidle")
        playwright_api.expect(ui.page.get_by_text(heading).first).to_be_visible()
    assert ui.unexpected_errors() == []


def test_review_approve_and_execute_through_the_ui(ui: UiSession, login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("Bug: search returns an error"))
    work_item = find_action(detail, "create_azure_devops_work_item")
    expect = playwright_api.expect

    ui.login_as(member)
    ui.page.goto(f"{BASE_URL}/actions/{work_item['id']}")
    expect(ui.page.get_by_role("heading", name="Original email")).to_be_visible()
    # Prompt-injection warning is shown and the untrusted text is rendered inertly as text.
    expect(ui.page.get_by_text(re.compile("injection", re.IGNORECASE)).first).to_be_visible()
    assert ui.page.locator("script:not([src])").count() == 0

    ui.page.get_by_role("button", name="Approve", exact=True).first.click()
    expect(ui.page.get_by_text(work_item["payload_hash"][:12]).first).to_be_visible()  # dialog shows bound hash
    ui.page.get_by_role("button", name="Confirm approval").click()
    expect(ui.page.get_by_text(re.compile(r"Approved by")).first).to_be_visible(timeout=15_000)

    ui.page.get_by_role("button", name=re.compile(r"^Execute")).first.click()
    ui.page.get_by_role("button", name="Execute now").click()
    expect(ui.page.get_by_text("Completed", exact=True).first).to_be_visible(timeout=60_000)
    expect(ui.page.get_by_text("Mock", exact=True).first).to_be_visible()
    # mock:// URLs are never rendered as links
    assert ui.page.locator("a[href^='mock://']").count() == 0
    assert member.action(work_item["id"])["status"] == "completed"
    assert ui.unexpected_errors() == []


def test_viewer_cannot_reach_another_users_action(ui: UiSession, login: Login) -> None:
    member = login("member")
    detail = member.analyze(member.email_by_subject("Feature request: saved filters"))
    action_id = detail["actions"][0]["id"]
    ui.login_as(login("viewer"))
    ui.page.goto(f"{BASE_URL}/actions/{action_id}")
    ui.page.wait_for_load_state("networkidle")
    assert ui.page.get_by_role("button", name="Approve", exact=True).count() == 0
    assert ui.page.get_by_role("button", name=re.compile(r"^Execute")).count() == 0


def test_inbox_sync_and_analyze_from_the_ui(ui: UiSession, login: Login) -> None:
    ui.login_as(login("member"))
    ui.page.goto(f"{BASE_URL}/inbox")
    ui.page.get_by_role("button", name="Sync mailbox").click()
    row_button = ui.page.get_by_role("button", name=re.compile(r"Analyze “Request: VPN access"))
    playwright_api.expect(row_button.or_(ui.page.get_by_text("Request: VPN access")).first).to_be_visible(
        timeout=20_000
    )
    if row_button.count():
        row_button.first.click()
        playwright_api.expect(ui.page.get_by_text(re.compile(r"Analysis queued|queued", re.I)).first).to_be_visible(
            timeout=10_000
        )


@pytest.mark.parametrize("path", ["/", "/inbox", "/approvals", "/workflows"])
def test_mobile_layout_has_no_horizontal_overflow(browser: Browser, login: Login, path: str) -> None:
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
    context.add_cookies(session_cookies(login("member")))
    page = context.new_page()
    try:
        page.goto(f"{BASE_URL}{path}")
        page.wait_for_load_state("networkidle")
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"{path} overflows horizontally by {overflow}px"
    finally:
        context.close()
