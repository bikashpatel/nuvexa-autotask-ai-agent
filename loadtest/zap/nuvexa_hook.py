"""ZAP packaged-scan hook used by scripts/dast.sh (runs inside the pinned ZAP container, Python 3.11).

Authenticated scans present a demo persona's session the way the dashboard does: the `nuvexa_session` +
`nuvexa_csrf` cookies on every request and the CSRF double-submit header `X-CSRF-Token` on every request
(harmless on safe methods, required on unsafe ones). Both are injected with ZAP replacer rules, so the
spider, the AJAX spider's browser, the OpenAPI importer and the active scanner all send them.

The sessions are created by `loadtest/zap/prepare_targets.py` before ZAP starts and read from
$NUVEXA_SESSION_FILE. Two routes end a session: `POST /auth/logout` revokes it and `POST /auth/demo-login`
revokes the session it is presented with. The replacer rules skip those two paths, so scanning them is
anonymous and cannot log the scan out; the scan still covers them.

The hook proves the session works before scanning (GET /auth/me → 200) and records whether it survived the
scan in $NUVEXA_SESSION_CHECK, so a scan that silently ran logged-out is visible in the summary.

For the SPA baseline it also imports $NUVEXA_HAR, the browser crawl recorded by loadtest/zap/spa_crawl.py
(ZAP's own browser crawlers do not start in this container on Apple silicon), so the passive scan covers the
pages and API calls the dashboard really makes.

Environment: NUVEXA_PERSONA (empty → unauthenticated scan), NUVEXA_TARGET (base URL as ZAP reaches it),
NUVEXA_SESSION_FILE, NUVEXA_SESSION_CHECK, NUVEXA_HAR.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlsplit

PERSONA = os.environ.get("NUVEXA_PERSONA", "")
TARGET = os.environ.get("NUVEXA_TARGET", "").rstrip("/")
SESSION_FILE = os.environ.get("NUVEXA_SESSION_FILE", "/zap/wrk/sessions.json")
SESSION_CHECK = os.environ.get("NUVEXA_SESSION_CHECK", "")
HAR = os.environ.get("NUVEXA_HAR", "")

# Java regex (ZAP): every URL except the two session-ending auth routes and anything below them. Variants
# such as `/auth/logout/` must be excluded too: FastAPI answers them with a 307 to `/auth/logout`, and ZAP
# follows the redirect with the original request's headers, which logged the first active scans out.
AUTHENTICATED_URLS = r"^(?!.*/api/v1/auth/(?:logout|demo-login)).*$"

log = logging.getLogger("nuvexa-hook")


def _session() -> dict[str, str]:
    with open(SESSION_FILE, encoding="utf-8") as handle:
        sessions: dict[str, dict[str, str]] = json.load(handle)
    return sessions[PERSONA]


def _status_of_me(zap: Any) -> int:
    """GET /api/v1/auth/me through ZAP (so the replacer rules apply) and return the status code."""
    parts = urlsplit(TARGET)
    raw = (
        f"GET {TARGET}/api/v1/auth/me HTTP/1.1\r\n"
        f"Host: {parts.netloc}\r\n"
        "Accept: application/json\r\n"
        "User-Agent: nuvexa-dast-session-check\r\n\r\n"
    )
    messages = zap.core.send_request(raw, followredirects="false")
    header = messages[-1]["responseHeader"] if messages else ""
    first_line = header.split("\r\n", 1)[0].split(" ")
    return int(first_line[1]) if len(first_line) > 1 and first_line[1].isdigit() else 0


def _record(text: str) -> None:
    if SESSION_CHECK:
        with open(SESSION_CHECK, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def zap_started(zap: Any, target: str) -> None:
    del target  # api-scan passes the OpenAPI file here; NUVEXA_TARGET is the base URL
    if not PERSONA:
        _record("persona=anonymous")
        return
    session = _session()
    cookie = f"nuvexa_session={session['session']}; nuvexa_csrf={session['csrf']}"
    for description, header, value in (
        ("nuvexa demo session cookies", "Cookie", cookie),
        ("nuvexa CSRF double-submit header", "X-CSRF-Token", session["csrf"]),
    ):
        zap.replacer.add_rule(
            description=description,
            enabled="true",
            matchtype="REQ_HEADER",
            matchregex="false",
            matchstring=header,
            replacement=value,
            initiators="",
            url=AUTHENTICATED_URLS,
        )
    status = _status_of_me(zap)
    _record(f"persona={PERSONA} before_scan_auth_me={status}")
    if status != 200:
        raise RuntimeError(f"the {PERSONA} session is not accepted (GET /auth/me -> {status}); refusing to scan")
    log.info("Authenticated as demo persona %s", PERSONA)


def zap_tuned(zap: Any) -> None:
    """After the packaged scan's tuning: import the browser crawl (loadtest/zap/spa_crawl.py) for passive scanning."""
    if HAR and os.path.exists(HAR):
        result = zap.exim.import_har(HAR)
        _record(f"har={os.path.basename(HAR)} import={result}")
        log.info("Imported browser crawl %s: %s", HAR, result)


def zap_pre_shutdown(zap: Any) -> None:
    if PERSONA:
        _record(f"persona={PERSONA} after_scan_auth_me={_status_of_me(zap)}")
