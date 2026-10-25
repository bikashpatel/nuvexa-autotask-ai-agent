"""Browser crawl of the dashboard for the ZAP SPA baseline in scripts/dast.sh.

ZAP's own browser crawlers (client spider, AJAX spider) cannot start Firefox in the ZAP container on
Docker Desktop for Apple silicon (OOM kill / "Failed to read marionette port"), so the SPA is crawled with
the repository's locked Playwright Chromium instead. The crawl signs in through the real login page (the
demo persona buttons), opens every dashboard route and the detail pages of seeded resources, and records
everything to a HAR file. The HAR's origin is rewritten to the URL ZAP scans, and the ZAP hook imports it,
so ZAP passively scans the same requests and responses the browser saw (the SPA's own API calls included).

Usage: uv run --locked --group e2e python loadtest/zap/spa_crawl.py --base-url http://127.0.0.1:38080 \
         --zap-base-url http://frontend:8080 --persona member --targets reports/dast/targets.json \
         --har reports/dast/spa-member.har
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROUTES = ("/", "/inbox", "/approvals", "/workflows", "/integrations", "/settings", "/audit", "/tasks", "/no-such-page")
TITLES = {"admin": "Admin", "approver": "Approver", "member": "Member", "viewer": "Viewer"}


def visit(page: Page, path: str) -> None:
    page.goto(path, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=15_000)


def crawl(base_url: str, persona: str, targets: dict[str, object], har: Path) -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(base_url=base_url, record_har_path=str(har), record_har_content="embed")
        page = context.new_page()
        visit(page, "/login")
        if persona != "anonymous":
            page.get_by_role("button", name=f"Sign in as demo {TITLES[persona]}").click()
            page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
        owned = targets["member"]  # type: ignore[index]
        details = (
            f"/emails/{owned['email_id']}",
            f"/workflows/{owned['run_id']}",
            *([f"/actions/{owned['action_id']}"] if owned.get("action_id") else []),
        )
        for path in (*ROUTES, *details):
            visit(page, path)
        visited = len(ROUTES) + len(details) + 1
        context.close()  # writes the HAR
        browser.close()
    return visited


def rewrite_origin(har: Path, base_url: str, zap_base_url: str) -> None:
    """Point the recorded traffic at the origin ZAP scans (its alerts are filtered by that base URL)."""
    if base_url.rstrip("/") == zap_base_url.rstrip("/"):
        return
    source, target = base_url.rstrip("/"), zap_base_url.rstrip("/")
    source_host, target_host = source.split("://", 1)[1], target.split("://", 1)[1]
    data = json.loads(har.read_text())
    for entry in data["log"]["entries"]:
        request = entry["request"]
        request["url"] = request["url"].replace(source, target, 1)
        for header in request["headers"]:
            if header["name"].lower() == "host":
                header["value"] = target_host
            elif header["name"].lower() in ("origin", "referer"):
                header["value"] = header["value"].replace(source, target, 1)
        for header in entry["response"]["headers"]:
            if header["name"].lower() == "location":
                header["value"] = header["value"].replace(source, target, 1)
    data["log"]["pages"] = [
        {**page, "title": page.get("title", "").replace(source_host, target_host)} for page in data["log"]["pages"]
    ]
    har.write_text(json.dumps(data))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--zap-base-url", required=True)
    parser.add_argument("--persona", required=True, choices=("anonymous", *TITLES))
    parser.add_argument("--targets", required=True, type=Path)
    parser.add_argument("--har", required=True, type=Path)
    args = parser.parse_args()
    targets = json.loads(args.targets.read_text())
    visited = crawl(args.base_url.rstrip("/"), args.persona, targets, args.har)
    rewrite_origin(args.har, args.base_url, args.zap_base_url)
    entries = len(json.loads(args.har.read_text())["log"]["entries"])
    print(f"crawled {visited} pages as {args.persona}: {entries} requests recorded in {args.har}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
