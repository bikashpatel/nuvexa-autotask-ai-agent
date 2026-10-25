"""Adversarial QA: HTTP security headers served by the dashboard's nginx (SPA + /api reverse proxy).

The config is an envsubst template (frontend/nginx.conf). These tests render it the way the image's
entrypoint does (substitute ``${API_UPSTREAM}`` / ``${NGINX_LOCAL_RESOLVERS}``), parse it into a
directive tree and assert the header policy, including nginx's ``add_header`` inheritance rule: a
location (or ``if``) that declares *any* ``add_header`` silently drops every inherited one.

No nginx binary or Docker is needed; this is a static check of the reviewed configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
NGINX_CONF = FRONTEND / "nginx.conf"

# Values the container entrypoint would substitute (Dockerfile ENV / 15-local-resolvers.envsh).
TEMPLATE_VARIABLES = {
    "API_UPSTREAM": "http://api:8000",
    "NGINX_LOCAL_RESOLVERS": "127.0.0.11",
    "NGINX_REAL_IP_FROM": "127.0.0.1/32",
}

SECURITY_HEADERS = (
    "content-security-policy",
    "x-content-type-options",
    "referrer-policy",
    "x-frame-options",
    "permissions-policy",
    "strict-transport-security",
)


# --- A minimal nginx config parser ----------------------------------------------------------------


@dataclass
class Directive:
    name: str
    args: list[str]
    block: list[Directive] | None = None
    parent: Directive | None = field(default=None, repr=False)


def _render_template(text: str) -> str:
    def substitute(match: re.Match[str]) -> str:
        return TEMPLATE_VARIABLES.get(match.group(1), match.group(0))

    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", substitute, text)


def _tokenize(text: str) -> list[str | tuple[str]]:
    """Words, quoted strings (as 1-tuples so an empty "" survives) and the punctuation { } ;."""
    tokens: list[str | tuple[str]] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char.isspace():
            i += 1
        elif char == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif char in "{};":
            tokens.append(char)
            i += 1
        elif char in "\"'":
            quote, i, value = char, i + 1, []
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                value.append(text[i])
                i += 1
            assert i < n, "unterminated quoted string in nginx.conf"
            tokens.append(("".join(value),))
            i += 1
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] not in "{};":
                i += 1
            tokens.append(text[start:i])
    return tokens


def _parse(tokens: list[str | tuple[str]]) -> list[Directive]:
    root: list[Directive] = []
    stack: list[tuple[list[Directive], Directive | None]] = [(root, None)]
    words: list[str] = []
    for token in tokens:
        if token == ";":
            assert words, "empty statement"
            current, parent = stack[-1]
            current.append(Directive(words[0], words[1:], parent=parent))
            words = []
        elif token == "{":
            assert words, "block without a name"
            current, parent = stack[-1]
            directive = Directive(words[0], words[1:], block=[], parent=parent)
            current.append(directive)
            assert directive.block is not None
            stack.append((directive.block, directive))
            words = []
        elif token == "}":
            assert not words, f"unterminated statement before '}}': {words}"
            stack.pop()
            assert stack, "unbalanced '}'"
        else:
            words.append(token[0] if isinstance(token, tuple) else token)
    assert len(stack) == 1, "unbalanced '{'"
    assert not words, f"trailing statement without ';': {words}"
    return root


def _walk(directives: list[Directive]):
    for directive in directives:
        yield directive
        if directive.block is not None:
            yield from _walk(directive.block)


@pytest.fixture(scope="module")
def config() -> list[Directive]:
    return _parse(_tokenize(_render_template(NGINX_CONF.read_text(encoding="utf-8"))))


@pytest.fixture(scope="module")
def server(config: list[Directive]) -> Directive:
    servers = [d for d in config if d.name == "server"]
    assert len(servers) == 1, "expected exactly one server block"
    return servers[0]


def _children(block_owner: Directive, name: str) -> list[Directive]:
    assert block_owner.block is not None
    return [d for d in block_owner.block if d.name == name]


def _server_headers(server: Directive) -> dict[str, Directive]:
    return {d.args[0].lower(): d for d in _children(server, "add_header")}


def _location(server: Directive, *args: str) -> Directive:
    matches = [d for d in _children(server, "location") if d.args == list(args)]
    assert len(matches) == 1, f"location {' '.join(args)} not found exactly once"
    return matches[0]


def _csp(server: Directive) -> dict[str, list[str]]:
    header = _server_headers(server)["content-security-policy"]
    policy: dict[str, list[str]] = {}
    for part in header.args[1].split(";"):
        words = part.split()
        if words:
            assert words[0].lower() not in policy, f"duplicate CSP directive {words[0]}"
            policy[words[0].lower()] = words[1:]
    return policy


def _map_lookup(config: list[Directive], variable: str, uri: str, status: int = 200) -> str:
    """Evaluate an nginx ``map <source> $variable`` the way nginx does: exact keys, then regexes in order.

    The source may combine ``$uri`` and ``$status`` (e.g. ``"$status:$uri"``); both are substituted for
    the request being modelled. ``status`` is the response status nginx has decided on when the header
    filter evaluates ``add_header`` (200 for a served file, 304 for a revalidation, 404 for a miss, …).
    """
    maps = [d for d in config if d.name == "map" and d.args[1:] == [variable]]
    assert len(maps) == 1, f"map for {variable} not found"
    assert maps[0].block is not None
    source_template = maps[0].args[0]
    assert "$uri" in source_template, f"map for {variable} must be keyed on $uri"
    values = {"uri": uri, "status": str(status)}

    def substitute(match: re.Match[str]) -> str:
        assert match.group(1) in values, f"unexpected map source variable ${match.group(1)}"
        return values[match.group(1)]

    source = re.sub(r"\$([a-z_]+)", substitute, source_template)
    # Map parameters (not key/value pairs).
    parameters = {"volatile", "hostnames", "include"}
    entries = [(d.name, d.args[0] if d.args else "") for d in maps[0].block if d.name not in parameters]
    default = next((value for key, value in entries if key == "default"), "")
    for key, value in entries:
        if key != "default" and not key.startswith("~") and key == source:
            return value
    for key, value in entries:
        if key.startswith("~*") and re.search(key[2:], source, re.IGNORECASE):
            return value
        if key.startswith("~") and not key.startswith("~*") and re.search(key[1:], source):
            return value
    return default


# --- Parser sanity --------------------------------------------------------------------------------


def test_parser_reads_the_whole_rendered_template(config: list[Directive], server: Directive) -> None:
    assert {d.name for d in config} >= {"map", "server"}
    assert [d.args for d in _children(server, "listen")] == [["8080"], ["[::]:8080"]]
    # envsubst variables were substituted; nginx's own runtime $variables were left alone.
    proxy_pass = next(d for d in _walk(config) if d.name == "proxy_pass")
    assert proxy_pass.args == ["$nuvexa_api_upstream"]
    upstream = next(d for d in _walk(config) if d.name == "set" and d.args[0] == "$nuvexa_api_upstream")
    assert upstream.args[1] == "http://api:8000"


# --- Version leak ---------------------------------------------------------------------------------


def test_server_tokens_off_no_nginx_version_leak(server: Directive) -> None:
    tokens = _children(server, "server_tokens")
    assert [d.args for d in tokens] == [["off"]]


def test_upstream_server_header_is_not_passed_through(config: list[Directive], server: Directive) -> None:
    api = _location(server, "/api/")
    assert not [d for d in _walk(config) if d.name == "proxy_pass_header"]
    hidden = {d.args[0].lower() for d in _children(api, "proxy_hide_header")}
    assert "server" in hidden
    assert "x-powered-by" not in {d.args[0].lower() for d in _children(server, "add_header")}


def test_no_directory_listing(config: list[Directive]) -> None:
    assert all(d.args != ["on"] for d in _walk(config) if d.name == "autoindex")


# --- Security headers on every response ------------------------------------------------------------


@pytest.mark.parametrize("header", SECURITY_HEADERS)
def test_security_header_is_set_for_all_responses_including_errors(server: Directive, header: str) -> None:
    headers = _server_headers(server)
    assert header in headers, f"{header} missing"
    # Without `always`, nginx omits the header on 4xx/5xx (e.g. the SPA's 404s, proxied API errors).
    assert headers[header].args[-1] == "always", f"{header} must use `always`"


def test_x_content_type_options_nosniff(server: Directive) -> None:
    assert _server_headers(server)["x-content-type-options"].args[1].lower() == "nosniff"


def test_referrer_policy_never_leaks_full_urls_cross_origin(server: Directive) -> None:
    value = _server_headers(server)["referrer-policy"].args[1].lower()
    assert value in {"no-referrer", "same-origin", "strict-origin", "strict-origin-when-cross-origin"}


def test_framing_is_denied(server: Directive) -> None:
    assert _server_headers(server)["x-frame-options"].args[1].upper() == "DENY"
    assert _csp(server)["frame-ancestors"] == ["'none'"]


def test_permissions_policy_disables_powerful_features(server: Directive) -> None:
    value = _server_headers(server)["permissions-policy"].args[1].replace(" ", "")
    features = dict(item.split("=", 1) for item in value.split(","))
    for feature in ("camera", "microphone", "geolocation", "payment", "usb"):
        assert features.get(feature) == "()", f"{feature} must be disabled"


def test_hsts_is_sent_for_a_year_without_preload(server: Directive) -> None:
    # The only public ingress (Azure Container Apps, infra/azure/main.bicep) terminates TLS and adds no HSTS, so
    # nginx sends it. Browsers ignore HSTS received over plain HTTP (RFC 6797 §8.1), so local HTTP stacks are
    # unaffected. No `preload`: submitting to browser preload lists is a decision for the domain owner.
    directives = dict(
        item.strip().partition("=")[::2]
        for item in _server_headers(server)["strict-transport-security"].args[1].lower().split(";")
    )
    assert int(directives["max-age"]) >= 31_536_000
    assert "preload" not in directives


def test_no_location_or_if_block_drops_the_inherited_security_headers(config: list[Directive]) -> None:
    """nginx inherits add_header only into blocks that declare none of their own."""
    offenders = []
    for block in _walk(config):
        if block.name not in {"location", "if"} or block.block is None:
            continue
        own = {d.args[0].lower() for d in block.block if d.name == "add_header"}
        if own and not set(SECURITY_HEADERS) <= own:
            offenders.append(f"{block.name} {' '.join(block.args)} declares {sorted(own)}")
    assert offenders == []


def test_api_proxy_hides_upstream_copies_of_headers_nginx_sets(server: Directive) -> None:
    api = _location(server, "/api/")
    hidden = {d.args[0].lower() for d in _children(api, "proxy_hide_header")}
    added = set(_server_headers(server)) - {"cache-control"}
    assert added <= hidden, f"duplicate headers on /api responses: {sorted(added - hidden)}"
    # The API's own `Cache-Control: no-store` (app/api/middleware.py) must reach the browser.
    assert "cache-control" not in hidden
    assert not [d for d in _children(api, "proxy_ignore_headers") if "Cache-Control" in d.args]


# --- Content-Security-Policy ----------------------------------------------------------------------

UNSAFE_SCRIPT_SOURCES = {
    "'unsafe-inline'",
    "'unsafe-eval'",
    "'unsafe-hashes'",
    "'wasm-unsafe-eval'",
    "*",
    "data:",
    "blob:",
    "filesystem:",
    "http:",
    "https:",
}


def test_csp_script_src_allows_only_same_origin_scripts(server: Directive) -> None:
    policy = _csp(server)
    script_src = policy.get("script-src", policy.get("default-src"))
    assert script_src is not None
    assert set(script_src) & UNSAFE_SCRIPT_SOURCES == set()
    assert script_src == ["'self'"]


def test_csp_core_lockdown_directives(server: Directive) -> None:
    policy = _csp(server)
    assert policy["default-src"] == ["'self'"]
    assert policy["object-src"] == ["'none'"]
    assert policy["base-uri"] in (["'self'"], ["'none'"])
    # form-action and frame-ancestors do not fall back to default-src.
    assert policy["form-action"] == ["'self'"]
    assert policy["frame-ancestors"] == ["'none'"]


@pytest.mark.parametrize("directive", ["connect-src", "img-src", "style-src", "font-src", "frame-src"])
def test_csp_fetch_directives_have_no_wildcard_or_remote_scheme_sources(server: Directive, directive: str) -> None:
    sources = set(_csp(server).get(directive, []))
    assert sources & {"*", "http:", "https:", "ws:", "wss:"} == set(), f"{directive}: {sorted(sources)}"


class _ScriptAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline_scripts = 0
        self.inline_handlers: list[str] = []
        self._in_inline_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name.lower() for name, _ in attrs}
        self.inline_handlers += [f"<{tag} {name}>" for name in names if name.startswith("on")]
        if tag == "script" and "src" not in names:
            self._in_inline_script = True
            self.inline_scripts += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_inline_script = False


@pytest.mark.parametrize("relative", ["index.html", "dist/index.html"])
def test_html_entry_points_are_compatible_with_script_src_self(relative: str) -> None:
    """script-src 'self' only holds if index.html has no inline <script> or on* attribute."""
    path = FRONTEND / relative
    if not path.exists():
        pytest.skip(f"{relative} not built")
    audit = _ScriptAudit()
    audit.feed(path.read_text(encoding="utf-8"))
    assert audit.inline_scripts == 0
    assert audit.inline_handlers == []


# --- Cache policy: index.html revalidates, hashed assets are immutable, the API owns its own --------


def test_index_html_and_spa_routes_are_revalidated(config: list[Directive], server: Directive) -> None:
    # SPA routes resolve to /index.html via try_files, so $uri is /index.html when headers are sent.
    fallback = _children(_location(server, "/"), "try_files")
    assert fallback
    assert fallback[0].args[-1] == "/index.html"
    for uri in ("/index.html", "/"):
        value = _map_lookup(config, "$nuvexa_cache_control", uri)
        assert "no-cache" in value or "no-store" in value, f"{uri}: {value!r}"
        assert "immutable" not in value


def test_hashed_assets_are_long_lived_and_immutable(config: list[Directive], server: Directive) -> None:
    value = _map_lookup(config, "$nuvexa_cache_control", "/assets/index-wueLltFk.js")
    assert "max-age=31536000" in value
    assert "immutable" in value
    # A missing hashed asset must never fall back to index.html (which would then be cached for a year).
    assert [d.args for d in _children(_location(server, "/assets/"), "try_files")] == [["$uri", "=404"]]


def test_api_responses_get_no_nginx_cache_control(config: list[Directive]) -> None:
    # An empty add_header value is not emitted, so the API's `no-store` is the only Cache-Control.
    assert _map_lookup(config, "$nuvexa_cache_control", "/api/v1/emails/abc") == ""
    assert _map_lookup(config, "$nuvexa_cache_control", "/api/v1/auth/me") == ""


# Regression (Prompt 5): error responses under /assets/ must never carry the one-year immutable policy.
# The Cache-Control header is added with `always` (so errors get a policy too) and the map used to be
# keyed on $uri alone: a 404 for a hashed asset requested during a rolling deploy (new index.html, old
# replica) was served "public, max-age=31536000, immutable", so a CDN or browser could pin the miss for
# a year. The policy must depend on the response status as well.
@pytest.mark.parametrize("status", [400, 403, 404, 405, 416, 500, 502, 503, 504])
def test_asset_error_responses_are_not_cacheable(config: list[Directive], status: int) -> None:
    value = _map_lookup(config, "$nuvexa_cache_control", "/assets/index-wueLltFk.js", status)
    assert "immutable" not in value
    assert "public" not in value
    assert "max-age" not in value
    assert "no-store" in value, f"{status}: {value!r}"


@pytest.mark.parametrize("status", [200, 206, 304])
def test_asset_success_and_revalidation_keep_the_immutable_policy(config: list[Directive], status: int) -> None:
    # A 304 updates the cached headers (RFC 9111 §4.3.4): it must repeat the long-lived policy, not
    # downgrade it. 206 is a byte-range response of the same immutable file.
    value = _map_lookup(config, "$nuvexa_cache_control", "/assets/index-wueLltFk.js", status)
    assert value == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("status", [200, 304, 404, 502])
def test_api_cache_control_is_left_to_the_api_for_every_status(config: list[Directive], status: int) -> None:
    assert _map_lookup(config, "$nuvexa_cache_control", "/api/v1/emails/abc", status) == ""


def test_favicon_errors_are_not_cached_for_a_day(config: list[Directive]) -> None:
    assert _map_lookup(config, "$nuvexa_cache_control", "/favicon.svg") == "public, max-age=86400"
    assert _map_lookup(config, "$nuvexa_cache_control", "/favicon.svg", 304) == "public, max-age=86400"
    missing = _map_lookup(config, "$nuvexa_cache_control", "/favicon.svg", 404)
    assert "max-age" not in missing
    assert "no-cache" in missing or "no-store" in missing


@pytest.mark.parametrize("status", [200, 304, 404, 500])
def test_spa_shell_is_revalidated_for_every_status(config: list[Directive], status: int) -> None:
    for uri in ("/index.html", "/", "/.env"):
        value = _map_lookup(config, "$nuvexa_cache_control", uri, status)
        assert value in {"no-cache", "no-store"}, f"{status} {uri}: {value!r}"


def test_cache_policy_map_is_volatile_so_a_late_status_change_is_seen(config: list[Directive]) -> None:
    # nginx caches a map variable per request. The range filter turns a 200 into a 416 (unsatisfiable
    # Range) *after* the headers filter evaluated the map, and the 416 then passes the headers filter
    # again: without `volatile` it re-used the cached 200 value and a 416 for a hashed asset was sent
    # "public, max-age=31536000, immutable" (observed with nginx 1.30 in the container image).
    maps = [d for d in config if d.name == "map" and d.args[1:] == ["$nuvexa_cache_control"]]
    assert len(maps) == 1
    assert maps[0].block is not None
    assert [d.args for d in maps[0].block if d.name == "volatile"] == [[]]


def test_cache_control_header_is_still_added_to_error_responses(server: Directive) -> None:
    # `always` stays: without it nginx would send no Cache-Control at all on a 404, and the
    # response could be cached heuristically. The map, not the absence of the header, decides.
    header = _server_headers(server)["cache-control"]
    assert header.args == ["Cache-Control", "$nuvexa_cache_control", "always"]


def test_api_proxy_does_not_compress_dynamic_responses(server: Directive) -> None:
    # BREACH-style length side channels on authenticated JSON: gzip must be off for /api/.
    assert [d.args for d in _children(_location(server, "/api/"), "gzip")] == [["off"]]


def test_hashed_assets_are_served_without_byte_ranges(server: Directive) -> None:
    # A 416 (unsatisfiable Range) passes the header filter twice and was sent two Cache-Control headers
    # (`immutable`, then `no-store`). Hashed JS/CSS/fonts never need ranges: `max_ranges 0` answers every
    # Range request with the full 200, so neither 206 nor 416 can occur under /assets/.
    assets = _location(server, "/assets/")
    assert [d.args for d in _children(assets, "max_ranges")] == [["0"]]


def test_the_client_address_is_taken_only_from_a_trusted_proxy(server: Directive) -> None:
    # Behind an ingress (Azure Container Apps) the TCP peer is the proxy; without realip every request would carry
    # the proxy's address, and per-IP sign-in limits would be shared by all users (one attacker locks out
    # everyone). X-Forwarded-For is honoured only from NGINX_REAL_IP_FROM; the right-most untrusted entry wins.
    assert [d.args for d in _children(server, "set_real_ip_from")] == [["127.0.0.1/32"]]  # rendered image default
    assert "set_real_ip_from  ${NGINX_REAL_IP_FROM};" in NGINX_CONF.read_text(encoding="utf-8")
    assert [d.args for d in _children(server, "real_ip_header")] == [["X-Forwarded-For"]]
    assert [d.args for d in _children(server, "real_ip_recursive")] == [["on"]]


def test_the_image_trusts_only_loopback_by_default_and_renders_the_variable() -> None:
    dockerfile = (FRONTEND / "Dockerfile").read_text()
    assert re.search(r"NGINX_REAL_IP_FROM=127\.0\.0\.1/32\b", dockerfile)  # no proxy in front: the peer is the client
    envsubst = re.search(r"NGINX_ENVSUBST_FILTER=(\S+)", dockerfile)
    assert envsubst is not None
    assert "NGINX_REAL_IP_FROM" in envsubst.group(1)
