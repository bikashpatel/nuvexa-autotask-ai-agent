#!/usr/bin/env bash
# DAST with OWASP ZAP against an ISOLATED local stack (docs/DAST_AND_LOAD_TEST.md).
#
#   scripts/dast.sh                        # build + start the isolated stack, run every scan, tear it down
#   DAST_PHASES=baseline scripts/dast.sh   # only the passive baseline scans ("baseline", "active" or both)
#   DAST_KEEP=1 scripts/dast.sh            # keep the stack running afterwards (for debugging)
#   DAST_BASE_URL=http://127.0.0.1:38080 scripts/dast.sh   # scan a stack you already started (yours only)
#   DAST_PHASES=active DAST_DISABLE_RATE_LIMITS=1 DAST_REPORTS=reports/dast-no-limits scripts/dast.sh
#                                          # supplementary active pass with the API rate limits off (isolated only)
#
# Phases:
#   baseline  passive scans, unauthenticated and as every demo persona ($DAST_PERSONAS):
#             * SPA: ZAP's spider plus a Playwright browser crawl of every dashboard page (recorded as a HAR
#               and imported into ZAP: ZAP's own browser crawlers cannot start Firefox in its container on
#               Apple silicon), so the passive rules see the pages and the API calls the SPA really makes;
#             * API: the OpenAPI description imported into ZAP (zap-api-scan.py -S, no attacks).
#   active    OpenAPI-driven active scans (zap-api-scan.py, Default Policy, every rule; see
#             loadtest/zap/api-scan-rules.conf), unauthenticated and as $DAST_ACTIVE_PERSONAS.
#
# Authentication: loadtest/zap/prepare_targets.py signs every persona in (POST /api/v1/auth/demo-login),
# seeds synthetic demo data and writes one OpenAPI file per persona with real resource ids; the ZAP hook
# (loadtest/zap/nuvexa_hook.py) sends the session cookie and the X-CSRF-Token header on every request.
#
# Safety:
#   * Default target: a throwaway Compose project ($DAST_COMPOSE_PROJECT, default nuvexa-dast) on loopback
#     ports ($DAST_WEB_PORT/$DAST_API_PORT/$DAST_DB_PORT, default 38080/38000/35432), removed with its
#     volumes afterwards. ZAP joins only an --internal Docker network holding that stack's frontend and api
#     containers, so it cannot reach anything else, and runs with -silent (no update checks, no telemetry).
#   * DAST_BASE_URL must be a loopback URL (override: DAST_ALLOW_NON_LOOPBACK=1) and not one of the ports
#     of a developer demo stack or the E2E stack ($DAST_PROTECTED_PORTS; override: DAST_ALLOW_SHARED_PORT=1).
#     Active scans write data (they approve, execute, edit settings): never point them at a stack you use.
#
# Reports: $DAST_REPORTS (default reports/dast/, ignored by git): one ZAP HTML + JSON report and log per
# scan, summary.md (alerts grouped by rule and URL pattern), compose.log. Exit 1 when a scan fails or any
# High-risk alert is found; other alerts are listed for triage (docs/DAST_AND_LOAD_TEST.md §2).
# Needs Docker, uv (Playwright from the locked e2e group) and python3 >= 3.12.
set -euo pipefail
cd "$(dirname "$0")/.."
unset VIRTUAL_ENV  # uv uses the project environment (.venv)

# Released 2026-10-16 (>= 7 days before it was pinned); multi-arch index digest.
ZAP_IMAGE="zaproxy/zap-stable:2.17.0@sha256:5ccfff9a411743ab2615136a2edd7416bec9ce41572fa94e935df38c14adb2b6"

PROJECT="${DAST_COMPOSE_PROJECT:-nuvexa-dast}"
WEB_PORT="${DAST_WEB_PORT:-38080}"
API_PORT="${DAST_API_PORT:-38000}"
DB_PORT="${DAST_DB_PORT:-35432}"
PHASES="${DAST_PHASES:-baseline active}"
PERSONAS="${DAST_PERSONAS:-viewer member approver admin}"
ACTIVE_PERSONAS="${DAST_ACTIVE_PERSONAS:-member admin}"
PROTECTED_PORTS="${DAST_PROTECTED_PORTS:-8080 8000 18080 18000 28080 28000}"
ZAP_HEAP="${DAST_ZAP_HEAP:-1g}"
ZAP_MEMORY="${DAST_ZAP_MEMORY:-3g}"
ACTIVE_MAX_MINUTES="${DAST_ACTIVE_MAX_MINUTES:-45}"
OUT="${DAST_REPORTS:-reports/dast}"
SCAN_NET="${PROJECT}-scan"

mkdir -p "$OUT"
OUT_ABS="$(cd "$OUT" && pwd)"
failures=()

die() { echo "dast: $*" >&2; exit 2; }

case "$PROJECT" in
  nuvexa | nuvexa-e2e) die "refusing Compose project '$PROJECT': it belongs to the demo or E2E stack" ;;
esac
for phase in $PHASES; do
  case "$phase" in baseline | active) ;; *) die "unknown phase '$phase' (baseline, active)" ;; esac
done

COMPOSE_FILES=(-f docker-compose.yml -f loadtest/compose.isolated.yml)
# Supplementary coverage pass only (isolated stack): the shipped rate limits answer most active-scan attacks on
# the review/execute/validate/sync routes with 429 (loadtest/zap/compose.no-rate-limits.yml).
if [ "${DAST_DISABLE_RATE_LIMITS:-0}" = "1" ]; then COMPOSE_FILES+=(-f loadtest/zap/compose.no-rate-limits.yml); fi
compose() { docker compose -p "$PROJECT" "${COMPOSE_FILES[@]}" "$@"; }

# Prints "<host> <port>" of a URL.
url_host_port() {
  python3 -I -c 'import sys, urllib.parse as u; p = u.urlsplit(sys.argv[1]); print(p.hostname or "", p.port or {"https": 443}.get(p.scheme, 80))' "$1"
}

# The same URL with its host replaced (keeps scheme, port and path).
url_with_host() {
  python3 -I -c 'import sys, urllib.parse as u; p = u.urlsplit(sys.argv[1]); print(p._replace(netloc=sys.argv[2] + (f":{p.port}" if p.port else "")).geturl())' "$1" "$2"
}

check_target() {
  local host port
  read -r host port < <(url_host_port "$1")
  case "$host" in
    127.* | localhost | ::1) ;;
    *) [ "${DAST_ALLOW_NON_LOOPBACK:-0}" = "1" ] || die "refusing non-loopback target '$1' (set DAST_ALLOW_NON_LOOPBACK=1 only for a stack you own)" ;;
  esac
  for protected in $PROTECTED_PORTS; do
    if [ "$port" = "$protected" ] && [ "${DAST_ALLOW_SHARED_PORT:-0}" != "1" ]; then
      die "refusing port $port: it is a demo/E2E stack port (set DAST_ALLOW_SHARED_PORT=1 only for a stack you own)"
    fi
  done
}

cleanup() {
  local status=$?
  # A scan interrupted mid-run leaves its ZAP container behind (docker run --rm removes it once stopped).
  read -r -a leftover <<< "$(docker ps -q --filter "name=^${PROJECT}-zap-" | tr '\n' ' ')"
  if [ "${#leftover[@]}" -gt 0 ]; then docker stop "${leftover[@]}" >/dev/null 2>&1 || true; fi
  if [ -z "${DAST_BASE_URL:-}" ]; then
    compose logs --no-color > "$OUT/compose.log" 2>&1 || true
    if [ "${DAST_KEEP:-0}" != "1" ]; then
      compose down -v --remove-orphans >/dev/null 2>&1 || true
      docker network rm "$SCAN_NET" >/dev/null 2>&1 || true
    fi
  fi
  exit "$status"
}

if [ -n "${DAST_BASE_URL:-}" ]; then
  [ "${DAST_DISABLE_RATE_LIMITS:-0}" = "1" ] && die "DAST_DISABLE_RATE_LIMITS applies to the isolated stack only"
  BASE_URL="${DAST_BASE_URL%/}"
  check_target "$BASE_URL"
  if [ "$(uname -s)" = "Linux" ]; then
    ZAP_NET=(--network host)
    ZAP_BASE_URL="$BASE_URL"
  else
    # Docker Desktop: the host's loopback is host.docker.internal inside containers.
    ZAP_NET=(--add-host "host.docker.internal:host-gateway")
    ZAP_BASE_URL="$(url_with_host "$BASE_URL" host.docker.internal)"
  fi
  echo "dast: scanning the existing stack at $BASE_URL (ZAP uses $ZAP_BASE_URL); active scans modify its data"
else
  export NUVEXA_WEB_PORT="$WEB_PORT" NUVEXA_API_PORT="$API_PORT" NUVEXA_DB_PORT="$DB_PORT"
  BASE_URL="http://127.0.0.1:${WEB_PORT}"
  check_target "$BASE_URL"
  trap cleanup EXIT
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker network rm "$SCAN_NET" >/dev/null 2>&1 || true
  compose up -d --build
  echo "Waiting for ${BASE_URL}/api/v1/health/ready ..."
  for _ in $(seq 1 90); do
    if curl -fsS "${BASE_URL}/api/v1/health/ready" >/dev/null 2>&1; then break; fi
    sleep 2
  done
  curl -fsS "${BASE_URL}/api/v1/health/ready" >/dev/null || die "the isolated stack did not become ready"
  docker network create --internal "$SCAN_NET" >/dev/null
  docker network connect --alias frontend "$SCAN_NET" "$(compose ps -q frontend)"
  docker network connect --alias api "$SCAN_NET" "$(compose ps -q api)"
  ZAP_NET=(--network "$SCAN_NET")
  ZAP_BASE_URL="http://frontend:8080"
fi

# ZAP's work directory is bind-mounted; on Linux the container user (zap, uid 1000) must be able to write it.
if [ "$(uname -s)" = "Linux" ]; then chmod 0777 "$OUT_ABS"; fi
rm -f "$OUT_ABS"/baseline-* "$OUT_ABS"/active-* "$OUT_ABS"/spa-*.har "$OUT_ABS"/summary.md

echo "== Preparing targets: demo sessions, synthetic demo data, per-persona OpenAPI files"
python3 -I loadtest/zap/prepare_targets.py --base-url "$BASE_URL" --zap-base-url "$ZAP_BASE_URL" --out "$OUT_ABS"

# -silent: no update checks, no telemetry. Two passive-scan threads keep ZAP's memory small enough for a busy
# Docker Desktop VM (with the default one thread per core it was OOM-killed while scanning the JS bundles).
ZAP_OPTS="-silent -Xmx${ZAP_HEAP} -config pscans.threads=${DAST_ZAP_PSCAN_THREADS:-2}"
ACTIVE_OPTS="-config scanner.threadPerHost=4 -config scanner.maxRuleDurationInMins=10 -config scanner.maxScanDurationInMins=${ACTIVE_MAX_MINUTES}"

# zap <scan name> <persona|anonymous> <HAR path inside the container or ""> <packaged scan and its arguments...>
zap() {
  local name="$1" persona="$2" har="$3" rc=0
  shift 3
  [ "$persona" = "anonymous" ] && persona=""
  echo "-- $name"
  docker run --rm --name "${PROJECT}-zap-${name}" "${ZAP_NET[@]}" --memory "$ZAP_MEMORY" \
    -v "$OUT_ABS:/zap/wrk:rw" -v "$PWD/loadtest/zap:/zap/hooks:ro" \
    -e NUVEXA_PERSONA="$persona" -e NUVEXA_TARGET="$ZAP_BASE_URL" \
    -e NUVEXA_SESSION_FILE=/zap/wrk/sessions.json -e NUVEXA_SESSION_CHECK="/zap/wrk/${name}.session.txt" \
    -e NUVEXA_HAR="$har" \
    "$ZAP_IMAGE" "$@" --hook=/zap/hooks/nuvexa_hook.py -J "${name}.json" -r "${name}.html" \
    > "$OUT/${name}.log" 2>&1 || rc=$?
  # Packaged-scan exit codes: 0 no alerts, 2 warnings only, 1 a FAIL rule matched (none are configured), 3 error.
  case "$rc" in
    0 | 2) grep -E '^(FAIL-NEW|WARN-NEW):' "$OUT/${name}.log" | sed 's/^/   /' || true ;;
    *)
      echo "   ZAP failed (exit $rc); see $OUT/${name}.log" >&2
      failures+=("$name")
      ;;
  esac
}

crawl() {
  local persona="$1"
  uv run --locked --group e2e python loadtest/zap/spa_crawl.py --base-url "$BASE_URL" --zap-base-url "$ZAP_BASE_URL" \
    --persona "$persona" --targets "$OUT_ABS/targets.json" --har "$OUT_ABS/spa-${persona}.har"
}

if [[ " $PHASES " == *" baseline "* ]]; then
  echo "== Baseline (passive) scans: SPA and API, unauthenticated and as: $PERSONAS"
  uv sync --locked --group e2e --quiet
  uv run --locked --group e2e playwright install chromium >/dev/null
  for persona in anonymous $PERSONAS; do
    crawl "$persona"
    zap "baseline-spa-$persona" "$persona" "/zap/wrk/spa-${persona}.har" zap-baseline.py -t "$ZAP_BASE_URL/" -m 1 -z "$ZAP_OPTS"
    zap "baseline-api-$persona" "$persona" "" zap-api-scan.py -t "/zap/wrk/openapi.${persona}.json" -f openapi -S -z "$ZAP_OPTS"
  done
fi

if [[ " $PHASES " == *" active "* ]]; then
  echo "== Active API scans (Default Policy, every rule), unauthenticated and as: $ACTIVE_PERSONAS"
  for persona in anonymous $ACTIVE_PERSONAS; do
    zap "active-api-$persona" "$persona" "" zap-api-scan.py -t "/zap/wrk/openapi.${persona}.json" -f openapi \
      -c /zap/hooks/api-scan-rules.conf -z "$ZAP_OPTS $ACTIVE_OPTS"
  done
fi

echo "== Summary ($OUT/summary.md)"
summary_rc=0
python3 -I - "$OUT_ABS" <<'PY' || summary_rc=$?
import json, re, sys
from collections import defaultdict
from pathlib import Path

out = Path(sys.argv[1])
RISK = {0: "Informational", 1: "Low", 2: "Medium", 3: "High"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
alerts = defaultdict(lambda: {"risk": 0, "confidence": set(), "scans": set(), "urls": set(), "instances": 0})
for report in sorted(out.glob("*.json")):
    if not report.stem.startswith(("baseline-", "active-")):
        continue
    for site in json.loads(report.read_text()).get("site", []):
        for alert in site.get("alerts", []):
            entry = alerts[(alert["pluginid"], alert["alert"])]
            entry["risk"] = max(entry["risk"], int(alert["riskcode"]))
            # riskdesc is "<Risk> (<Confidence>)", e.g. "Medium (High)".
            entry["confidence"].add(alert.get("riskdesc", "").partition("(")[2].rstrip(")") or "?")
            entry["scans"].add(report.stem)
            for instance in alert.get("instances", []):
                path = re.sub(r"^https?://[^/]+", "", instance.get("uri", ""))
                entry["urls"].add(f'{instance.get("method", "GET")} {UUID.sub("{id}", path.split("?")[0])}')
                entry["instances"] += 1
rows = sorted(alerts.items(), key=lambda kv: (-kv[1]["risk"], kv[0][1]))
lines = ["| Risk | Alert (ZAP rule) | Confidence | Instances | Scans | URL patterns |", "|---|---|---|---|---|---|"]
for (plugin, name), e in rows:
    urls = sorted(e["urls"])
    shown = "<br>".join(urls[:6]) + (f"<br>… {len(urls) - 6} more" if len(urls) > 6 else "")
    lines.append(f'| {RISK[e["risk"]]} | {name} ({plugin}) | {", ".join(sorted(e["confidence"]))} | {e["instances"]} | {len(e["scans"])} | {shown} |')
checks = [f"* `{p.name}`: " + p.read_text().strip().replace("\n", "; ") for p in sorted(out.glob("*.session.txt"))]
(out / "summary.md").write_text("\n".join(["# ZAP alerts (all scans)", "", *lines, "", "## Session checks", "", *checks, ""]))
for (plugin, name), e in rows:
    print(f'  {RISK[e["risk"]]:<13} {name} ({plugin}): {e["instances"]} instance(s) in {len(e["scans"])} scan(s)')
lost = [c for c in checks if "after_scan_auth_me=" in c and "after_scan_auth_me=200" not in c]
if lost:
    print("  WARNING: a scan lost its session (coverage reduced):", *lost, sep="\n    ")
sys.exit(1 if any(e["risk"] >= 3 for e in alerts.values()) else 0)
PY

if [ "${#failures[@]}" -gt 0 ]; then
  echo "dast: ${#failures[@]} scan(s) failed: ${failures[*]}" >&2
  exit 1
fi
if [ "$summary_rc" -ne 0 ]; then
  echo "dast: High-risk alert(s) found; see $OUT/summary.md" >&2
  exit 1
fi
echo "dast: all scans completed; triage the alerts in $OUT/summary.md (docs/DAST_AND_LOAD_TEST.md)"
