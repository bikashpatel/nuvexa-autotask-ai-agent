#!/usr/bin/env bash
# Load smoke test with k6 against an ISOLATED local stack (docs/DAST_AND_LOAD_TEST.md §3).
#
#   scripts/load-smoke.sh                       # build + start the isolated stack, seed, run, tear it down
#   LOAD_KEEP=1 scripts/load-smoke.sh           # keep the stack running afterwards
#   K6_PROFILE=quick scripts/load-smoke.sh      # 30-second check of the script itself (no measurement)
#   LOAD_BASE_URL=http://127.0.0.1:39080 LOAD_COMPOSE_PROJECT=<yours> scripts/load-smoke.sh   # existing stack
#
# Knobs: LOAD_MAX_VUS (25), LOAD_FLOW_RATE (analyze/approve/execute flows per minute, 6), LOAD_HOLD
# (steady state, 3m), LOAD_SEED_COPIES (synthetic copies of every synced demo email, 100 -> ~3,000 emails;
# 0 = only the 42-message demo mailboxes).
#
# What it records (reports in $LOAD_REPORTS, default reports/load/, ignored by git):
#   k6-summary.json / k6.log   client-side latency per endpoint group (p50/p95/p99), error rate, 429s, flow timings
#   jobs.txt                   worker job latency (queued -> claimed -> done) per job type, from the jobs table
#   pg-activity.csv            database connections (total / active / idle in transaction / waiting) every 5 s
#   docker-stats.csv           CPU and memory of the stack's containers every 10 s
#   server.txt                 API log analysis: statuses, server-side latency per route, errors, pool timeouts
#   summary.md                 all of the above in one file
#
# Safety: the default target is a throwaway Compose project ($LOAD_COMPOSE_PROJECT, default nuvexa-load) on
# loopback ports ($LOAD_WEB_PORT/$LOAD_API_PORT/$LOAD_DB_PORT, default 39080/39000/39432), removed with its
# volumes afterwards. k6 runs from a pinned image on an --internal Docker network that holds only the stack's
# frontend, so it cannot load anything else (and --no-usage-report: no telemetry). LOAD_BASE_URL must be a
# loopback URL (override: LOAD_ALLOW_NON_LOOPBACK=1) and not a demo/E2E port (LOAD_PROTECTED_PORTS;
# override LOAD_ALLOW_SHARED_PORT=1): the flows approve and execute actions.
# Exit status: k6's (99 = a threshold failed), or 1 when the API/worker logs show pool timeouts or 5xx.
set -euo pipefail
cd "$(dirname "$0")/.."

# Released 2026-08-10 (>= 7 days before it was pinned); multi-arch index digest.
K6_IMAGE="grafana/k6:2.2.0@sha256:9bd01d6941fca969cb61bb57d2da5ee9b385fe2aa8881df3798c196564d6ace6"

PROJECT="${LOAD_COMPOSE_PROJECT:-nuvexa-load}"
WEB_PORT="${LOAD_WEB_PORT:-39080}"
API_PORT="${LOAD_API_PORT:-39000}"
DB_PORT="${LOAD_DB_PORT:-39432}"
SEED_COPIES="${LOAD_SEED_COPIES:-100}"
PROTECTED_PORTS="${LOAD_PROTECTED_PORTS:-8080 8000 18080 18000 28080 28000}"
OUT="${LOAD_REPORTS:-reports/load}"
NET="${PROJECT}-k6"

mkdir -p "$OUT"
OUT_ABS="$(cd "$OUT" && pwd)"
rm -f "$OUT_ABS"/k6-summary.json "$OUT_ABS"/*.csv "$OUT_ABS"/*.txt "$OUT_ABS"/summary.md "$OUT_ABS"/*.log
samplers=()

die() { echo "load-smoke: $*" >&2; exit 2; }
compose() { docker compose -p "$PROJECT" -f docker-compose.yml -f loadtest/compose.isolated.yml "$@"; }
psql_q() { compose exec -T postgres psql -U nuvexa -d nuvexa -v ON_ERROR_STOP=1 -tA "$@"; }

case "$PROJECT" in
  nuvexa | nuvexa-e2e) die "refusing Compose project '$PROJECT': it belongs to the demo or E2E stack" ;;
esac

check_target() {
  local host port
  read -r host port < <(python3 -I -c 'import sys, urllib.parse as u; p = u.urlsplit(sys.argv[1]); print(p.hostname or "", p.port or {"https": 443}.get(p.scheme, 80))' "$1")
  case "$host" in
    127.* | localhost | ::1) ;;
    *) [ "${LOAD_ALLOW_NON_LOOPBACK:-0}" = "1" ] || die "refusing non-loopback target '$1' (set LOAD_ALLOW_NON_LOOPBACK=1 only for a stack you own)" ;;
  esac
  for protected in $PROTECTED_PORTS; do
    if [ "$port" = "$protected" ] && [ "${LOAD_ALLOW_SHARED_PORT:-0}" != "1" ]; then
      die "refusing port $port: it is a demo/E2E stack port (set LOAD_ALLOW_SHARED_PORT=1 only for a stack you own)"
    fi
  done
}

cleanup() {
  local status=$?
  # ${a[@]+...}: bash 3.2 (macOS /bin/bash) treats an empty array as unset under `set -u`.
  for pid in ${samplers[@]+"${samplers[@]}"}; do kill "$pid" 2>/dev/null || true; done
  compose logs --no-color > "$OUT/compose.log" 2>&1 || true
  if [ -z "${LOAD_BASE_URL:-}" ] && [ "${LOAD_KEEP:-0}" != "1" ]; then
    docker network disconnect -f "$NET" "$(compose ps -q frontend 2>/dev/null)" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    compose down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

if [ -n "${LOAD_BASE_URL:-}" ]; then
  BASE_URL="${LOAD_BASE_URL%/}"
  check_target "$BASE_URL"
  # Job, pool and log metrics need the stack's Compose project; k6 reaches the host's loopback.
  [ -n "${LOAD_COMPOSE_PROJECT:-}" ] || die "LOAD_BASE_URL needs LOAD_COMPOSE_PROJECT (the stack's Compose project)"
  if [ "$(uname -s)" = "Linux" ]; then
    K6_NET=(--network host)
    K6_BASE_URL="$BASE_URL"
  else
    K6_NET=(--add-host "host.docker.internal:host-gateway")
    K6_BASE_URL="$(python3 -I -c 'import sys, urllib.parse as u; p = u.urlsplit(sys.argv[1]); print(p._replace(netloc="host.docker.internal" + (f":{p.port}" if p.port else "")).geturl())' "$BASE_URL")"
  fi
else
  export NUVEXA_WEB_PORT="$WEB_PORT" NUVEXA_API_PORT="$API_PORT" NUVEXA_DB_PORT="$DB_PORT"
  BASE_URL="http://127.0.0.1:${WEB_PORT}"
  check_target "$BASE_URL"
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  compose up -d --build
  echo "Waiting for ${BASE_URL}/api/v1/health/ready ..."
  for _ in $(seq 1 90); do
    if curl -fsS "${BASE_URL}/api/v1/health/ready" >/dev/null 2>&1; then break; fi
    sleep 2
  done
  curl -fsS "${BASE_URL}/api/v1/health/ready" >/dev/null || die "the isolated stack did not become ready"
  docker network create --internal "$NET" >/dev/null
  docker network connect --alias frontend "$NET" "$(compose ps -q frontend)"
  K6_NET=(--network "$NET")
  K6_BASE_URL="http://frontend:8080"
fi

# --- Synthetic volume: sync every demo mailbox, then copy each synced email $SEED_COPIES times ----------
if [ "$SEED_COPIES" -gt 0 ]; then
  echo "== Seeding synthetic emails ($SEED_COPIES copies of every synced demo email)"
  jar="$(mktemp)"
  for persona in member approver admin; do
    : > "$jar"
    curl -fsS -c "$jar" -H 'Content-Type: application/json' -d "{\"persona\":\"$persona\"}" \
      "$BASE_URL/api/v1/auth/demo-login" >/dev/null
    csrf="$(awk '$6 == "nuvexa_csrf" {print $7}' "$jar")"
    curl -fsS -b "$jar" -H "X-CSRF-Token: $csrf" -H 'Content-Type: application/json' -d '{}' \
      "$BASE_URL/api/v1/emails/sync" >/dev/null
  done
  rm -f "$jar"
  for _ in $(seq 1 60); do
    synced="$(psql_q -c "select count(*) from (select connected_account_id from email_messages group by 1 having count(*) >= 10) s")"
    [ "$synced" -ge 3 ] && break
    sleep 1
  done
  psql_q -c "
    insert into email_messages (id, created_at, updated_at, tenant_id, owner_user_id, connected_account_id,
      provider_message_id, conversation_id, subject, sender_name, sender_address, to_recipients, cc_recipients,
      received_at, sent_at, body_text, body_preview, body_content_type, has_attachments, attachments,
      importance, is_read, content_hash, analysis_status, content_changed_since_analysis)
    select gen_random_uuid(), now(), now(), e.tenant_id, e.owner_user_id, e.connected_account_id,
      e.provider_message_id || ':load-' || g, e.conversation_id || ':load-' || g,
      e.subject || ' [synthetic copy ' || g || ']', e.sender_name, e.sender_address, e.to_recipients,
      e.cc_recipients, e.received_at - make_interval(mins => g), e.sent_at, e.body_text, e.body_preview,
      e.body_content_type, e.has_attachments, e.attachments, e.importance, e.is_read,
      encode(sha256(convert_to(e.content_hash || ':' || g, 'UTF8')), 'hex'), 'not_analyzed', false
    from email_messages e cross join generate_series(1, $SEED_COPIES) g
    where e.provider_message_id not like '%:load-%'" >/dev/null
  echo "   $(psql_q -c 'select count(*) from email_messages') emails in the database"
fi

# --- Samplers: database connections every 5 s, container CPU/memory every 10 s ---------------------------
echo "epoch,total,active,idle,idle_in_transaction,waiting_on_lock" > "$OUT/pg-activity.csv"
(
  while true; do
    row="$(psql_q -F, -c "select extract(epoch from now())::int, count(*), count(*) filter (where state = 'active'),
      count(*) filter (where state = 'idle'), count(*) filter (where state like 'idle in transaction%'),
      count(*) filter (where wait_event_type = 'Lock') from pg_stat_activity where datname = 'nuvexa'" 2>/dev/null)" || true
    [ -n "$row" ] && echo "$row" >> "$OUT/pg-activity.csv"
    sleep 5
  done
) &
samplers+=("$!")
disown "$!"  # no "Terminated" job report when it is stopped
echo "epoch,container,cpu,mem" > "$OUT/docker-stats.csv"
(
  while true; do
    now="$(date +%s)"
    read -r -a containers <<< "$(compose ps -q | tr '\n' ' ')"
    docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' ${containers[@]+"${containers[@]}"} 2>/dev/null \
      | sed "s/^/${now},/" >> "$OUT/docker-stats.csv" || true
    sleep 10
  done
) &
samplers+=("$!")
disown "$!"  # no "Terminated" job report when it is stopped

# --- k6 --------------------------------------------------------------------------------------------------
started="$(psql_q -c "select to_char(now() at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')")"
echo "== k6 ($K6_IMAGE) against $K6_BASE_URL, started $started"
k6_rc=0
docker run --rm --name "${PROJECT}-k6" "${K6_NET[@]}" -v "$PWD/loadtest:/scripts:ro" -v "$OUT_ABS:/out:rw" \
  -e BASE_URL="$K6_BASE_URL" -e MAX_VUS="${LOAD_MAX_VUS:-25}" -e FLOW_RATE="${LOAD_FLOW_RATE:-6}" \
  -e HOLD="${LOAD_HOLD:-3m}" -e PROFILE="${K6_PROFILE:-smoke}" -e SUMMARY_PATH=/out/k6-summary.json \
  "$K6_IMAGE" run --no-usage-report --quiet /scripts/smoke.js 2>&1 | tee "$OUT/k6.log" || k6_rc=$?
for pid in ${samplers[@]+"${samplers[@]}"}; do kill "$pid" 2>/dev/null || true; done
samplers=()

# --- Worker job latency (from the queue table) -----------------------------------------------------------
# locked_at is the claim time (heartbeats refresh it only every JOB_LEASE_SECONDS / 3 = 100 s), so
# locked_at - run_after is the queue wait and finished_at - created_at the end-to-end job latency.
{
  echo "job | status | n | queue wait p50 (s) | wait p95 | queued->done p50 (s) | p95 | p99 | max"
  psql_q -F ' | ' -c "
  select job_type || coalesce('/' || (payload->'command'->>'type'), '') as job, status, count(*) as n,
    round(percentile_cont(0.5) within group (order by extract(epoch from locked_at - run_after))::numeric, 2) as wait_p50_s,
    round(percentile_cont(0.95) within group (order by extract(epoch from locked_at - run_after))::numeric, 2) as wait_p95_s,
    round(percentile_cont(0.5) within group (order by extract(epoch from finished_at - created_at))::numeric, 2) as done_p50_s,
    round(percentile_cont(0.95) within group (order by extract(epoch from finished_at - created_at))::numeric, 2) as done_p95_s,
    round(percentile_cont(0.99) within group (order by extract(epoch from finished_at - created_at))::numeric, 2) as done_p99_s,
    round(max(extract(epoch from finished_at - created_at))::numeric, 2) as done_max_s
  from jobs where created_at >= '$started' group by 1, 2 order by 1, 2"
} > "$OUT/jobs.txt"

# --- Server-side view: API/worker logs since the start ----------------------------------------------------
compose logs --no-color --no-log-prefix --since "$started" api > "$OUT/api.log" 2>&1 || true
compose logs --no-color --no-log-prefix --since "$started" worker > "$OUT/worker.log" 2>&1 || true
server_rc=0
python3 -I - "$OUT_ABS" <<'PY' || server_rc=$?
import collections, json, re, sys
from pathlib import Path

out = Path(sys.argv[1])
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
POOL = re.compile(r"QueuePool limit|TimeoutError|too many (clients|connections)|connection timed out|PoolTimeout", re.I)
statuses, durations, events, pool_hits = collections.Counter(), collections.defaultdict(list), collections.Counter(), []
for name in ("api.log", "worker.log"):
    for line in (out / name).read_text(errors="replace").splitlines():
        if POOL.search(line):
            pool_hits.append(f"{name}: {line[:300]}")
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("level") in ("error", "critical", "warning"):
            events[(name, record.get("level"), record.get("event"))] += 1
        if record.get("event") == "http_request":
            route = f'{record.get("method")} {UUID.sub("{id}", record.get("path", ""))}'
            statuses[record.get("status")] += 1
            durations[route].append(int(record.get("duration_ms") or 0))


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


lines = ["Server-side (API http_request log events, duration_ms measured in the API process)", ""]
lines.append("status counts: " + ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items(), key=lambda kv: str(kv[0]))))
lines += ["", f'{"route":<58}{"n":>7}{"p50":>7}{"p95":>7}{"p99":>7}{"max":>7}']
for route, values in sorted(durations.items(), key=lambda kv: -len(kv[1])):
    lines.append(f"{route[:57]:<58}{len(values):>7}{pct(values, .5):>7}{pct(values, .95):>7}{pct(values, .99):>7}{max(values):>7}")
lines += ["", "warning/error log events:"] + [f"  {n:>6}  {f}/{lvl}: {ev}" for (f, lvl, ev), n in events.most_common()]
lines += ["", f"database pool / connection timeouts: {len(pool_hits)}"] + [f"  {h}" for h in pool_hits[:20]]
(out / "server.txt").write_text("\n".join(lines) + "\n")
print("\n".join(lines[:3]))
server_errors = sum(v for k, v in statuses.items() if isinstance(k, int) and k >= 500)
print(f"5xx responses: {server_errors}, pool timeouts: {len(pool_hits)}")
sys.exit(1 if server_errors or pool_hits else 0)
PY

pg_max="$(awk -F, 'NR > 1 && $2 > m {m = $2} END {print m + 0}' "$OUT/pg-activity.csv")"
pg_active_max="$(awk -F, 'NR > 1 && $3 > m {m = $3} END {print m + 0}' "$OUT/pg-activity.csv")"
pg_waiting_max="$(awk -F, 'NR > 1 && $6 > m {m = $6} END {print m + 0}' "$OUT/pg-activity.csv")"
python3 -I - "$OUT_ABS" > "$OUT/containers.txt" <<'PY'
import collections, csv, sys
from pathlib import Path

samples = collections.defaultdict(list)
with open(Path(sys.argv[1]) / "docker-stats.csv") as handle:
    for row in list(csv.reader(handle))[1:]:
        if len(row) == 4 and row[2].endswith("%"):
            samples[row[1]].append((float(row[2].rstrip("%")), row[3].split("/")[0].strip()))
print(f'{"container":<28}{"samples":>8}{"cpu p50":>9}{"cpu max":>9}  memory (last)')
for name, values in sorted(samples.items()):
    cpu = sorted(c for c, _ in values)
    print(f"{name:<28}{len(values):>8}{cpu[len(cpu) // 2]:>8.1f}%{cpu[-1]:>8.1f}%  {values[-1][1]}")
PY
{
  echo "# Load smoke run ($started, profile ${K6_PROFILE:-smoke})"
  echo
  echo '```'
  grep -v '^\s*$' "$OUT/k6.log" | tail -n 40
  echo '```'
  echo
  echo "## Worker jobs"
  echo
  echo '```'
  cat "$OUT/jobs.txt"
  echo '```'
  echo
  echo "## Database connections (pg_stat_activity, 5 s samples)"
  echo
  echo "max total $pg_max, max active $pg_active_max, max waiting on a lock $pg_waiting_max"
  echo
  echo "## Containers (docker stats, 10 s samples)"
  echo
  echo '```'
  cat "$OUT/containers.txt"
  echo '```'
  echo
  echo "## Server side"
  echo
  echo '```'
  cat "$OUT/server.txt"
  echo '```'
} > "$OUT/summary.md"
echo "== Database connections: max $pg_max (max active $pg_active_max, waiting on a lock $pg_waiting_max)"
cat "$OUT/jobs.txt" "$OUT/containers.txt"
echo "== Summary: $OUT/summary.md"

if [ "$k6_rc" -ne 0 ]; then
  echo "load-smoke: k6 exited $k6_rc (99 = a threshold failed)" >&2
  exit "$k6_rc"
fi
if [ "$server_rc" -ne 0 ]; then
  echo "load-smoke: the API/worker logs show 5xx responses or pool timeouts; see $OUT/server.txt" >&2
  exit 1
fi
echo "load-smoke: passed"
