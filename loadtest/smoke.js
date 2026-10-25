// k6 load smoke test for the NUVEXA demo stack (synthetic data, heuristic agent, mock Azure DevOps).
//
// Run it with scripts/load-smoke.sh, which starts an isolated Compose stack, runs this script in the
// pinned grafana/k6 image on a network without internet access, and collects worker/database metrics.
// Never point it at a shared stack: the flows scenario approves and executes actions.
//
// Scenarios (about 7.5 minutes in total):
//   browse  ramping VUs (0 -> 10 -> MAX_VUS, hold, ramp down). Each iteration is one dashboard page view
//           with the API calls that page makes: overview, inbox list/search, email detail, workflows,
//           approvals, action detail + history, audit log, tasks. Now and then a user clicks "Sync" or
//           signs in and out again.
//   flows   a small constant arrival rate of the human-in-the-loop flow on demo data: analyze an email,
//           wait for the proposal, approve it (version + payload hash) and execute it, wait for the result.
//
// Sessions: the demo login is rate limited per client IP (20/min), so setup() signs in each persona
// SESSIONS_PER_PERSONA times and the VUs share those sessions (cookie + X-CSRF-Token), like several browser
// tabs. Per-user rate limits (sync 10/min, analyze 60/min, review 120/min, execute 30/min) therefore apply
// to the shared users and some 429s are expected; they are counted in `rate_limited` and kept out of the
// error rate and the latency trends.
//
// Environment: BASE_URL (default http://frontend:8080), MAX_VUS (25), FLOW_RATE (flows per minute, 6),
// HOLD (steady-state duration, 3m), PROFILE (smoke | quick: a 30-second check of the script itself),
// SUMMARY_PATH (where handleSummary writes the JSON, /out/k6-summary.json), DEBUG (log unexpected responses).
import http from 'k6/http';
import { sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

const BASE_URL = (__ENV.BASE_URL || 'http://frontend:8080').replace(/\/+$/, '');
const API = `${BASE_URL}/api/v1`;
const MAX_VUS = parseInt(__ENV.MAX_VUS || '25', 10);
const FLOW_RATE = parseInt(__ENV.FLOW_RATE || '6', 10);
const HOLD = __ENV.HOLD || '3m';
const SUMMARY_PATH = __ENV.SUMMARY_PATH || '/out/k6-summary.json';
const QUICK = __ENV.PROFILE === 'quick'; // 30-second script check, not a measurement
const SESSIONS_PER_PERSONA = 2;
const PERSONAS = ['member', 'approver', 'admin', 'viewer'];
const MAILBOX_PERSONAS = ['member', 'approver', 'admin'];
const ANALYSIS_IN_FLIGHT = new Set(['received', 'normalized', 'classified', 'extracted', 'validated', 'action_proposed']);
const ACTION_TERMINAL = new Set(['completed', 'failed', 'uncertain', 'rejected', 'cancelled', 'superseded']);
const SEARCH_TERMS = ['urgent', 'invoice', 'access', 'bug', 'report', 'VPN', 'export', 'zzz-no-match'];

// Endpoint groups: latency (non-429 responses only) per group.
const GROUPS = [
  'auth_me', 'dashboard', 'inbox_list', 'inbox_search', 'email_detail', 'workflows_list', 'workflow_detail',
  'actions_list', 'action_detail', 'action_history', 'audit_log', 'tasks', 'integrations', 'login', 'logout',
  'sync', 'analyze', 'approve', 'execute',
];
const latency = Object.fromEntries(GROUPS.map((g) => [g, new Trend(`latency_${g}`, true)]));
const rateLimited = new Counter('rate_limited');
const rateLimitedByGroup = Object.fromEntries(GROUPS.map((g) => [g, new Counter(`rate_limited_${g}`)]));
const unexpected = new Rate('unexpected_responses'); // the error rate: anything not in the expected set
const conflicts = new Counter('flow_conflicts'); // 409: another VU moved the action/workflow first
const flowAnalyzeSeconds = new Trend('flow_analyze_seconds'); // POST analyze -> run leaves the analysis phase
const flowExecuteSeconds = new Trend('flow_execute_seconds'); // POST execute -> action reaches an outcome
const flowsCompleted = new Counter('flows_completed');
const flowsWithoutProposal = new Counter('flows_without_proposal');
const executionOutcomes = new Counter('execution_outcome_not_completed');

export const options = {
  setupTimeout: '3m',
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max', 'count'],
  scenarios: {
    browse: {
      executor: 'ramping-vus',
      exec: 'browse',
      startVUs: 0,
      stages: QUICK
        ? [
            { duration: '10s', target: Math.min(3, MAX_VUS) },
            { duration: '20s', target: Math.min(3, MAX_VUS) },
          ]
        : [
            { duration: '1m', target: Math.min(10, MAX_VUS) },
            { duration: '2m', target: MAX_VUS },
            { duration: HOLD, target: MAX_VUS },
            { duration: '1m', target: 0 },
          ],
      gracefulRampDown: '30s',
    },
    flows: {
      executor: 'constant-arrival-rate',
      exec: 'flow',
      rate: FLOW_RATE,
      timeUnit: '1m',
      duration: QUICK ? '25s' : '6m',
      startTime: QUICK ? '5s' : '30s',
      preAllocatedVUs: 4,
      maxVUs: 8,
    },
  },
  // Baseline thresholds for this smoke profile (docs/DAST_AND_LOAD_TEST.md §3.4). Set from the first recorded
  // runs on a developer laptop (macOS, Docker Desktop, shared with other workloads) at roughly 10x the observed
  // p95, so they catch order-of-magnitude regressions (an N+1 query, a lost index, pool exhaustion), not
  // drift. They are a baseline, not an SLA.
  thresholds: {
    unexpected_responses: ['rate<0.01'], // the error rate: 429 and the documented 409s are expected outcomes
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(99)<500'],
    latency_auth_me: ['p(95)<100'],
    latency_dashboard: ['p(95)<250'],
    latency_inbox_list: ['p(95)<250'],
    latency_inbox_search: ['p(95)<250'],
    latency_email_detail: ['p(95)<150'],
    latency_workflows_list: ['p(95)<250'],
    latency_workflow_detail: ['p(95)<250'],
    latency_actions_list: ['p(95)<300'],
    latency_action_detail: ['p(95)<250'],
    latency_action_history: ['p(95)<150'],
    latency_audit_log: ['p(95)<250'],
    latency_tasks: ['p(95)<250'],
    latency_login: ['p(95)<250'],
    latency_sync: ['p(95)<250'],
    latency_analyze: ['p(95)<300'],
    latency_approve: ['p(95)<300'],
    latency_execute: ['p(95)<300'],
    flow_analyze_seconds: ['p(95)<10'], // POST analyze -> proposal ready (worker poll interval 1 s)
    flow_execute_seconds: ['p(95)<15'], // POST execute -> outcome (worker + mock Azure DevOps)
    flows_completed: [QUICK ? 'count>=1' : 'count>=10'], // the human-in-the-loop flow really ran
  },
};

// ------------------------------------------------------------------------------------------------ helpers

function authHeaders(session) {
  return {
    Cookie: `nuvexa_session=${session.session}; nuvexa_csrf=${session.csrf}`,
    'X-CSRF-Token': session.csrf,
    Accept: 'application/json',
    'Content-Type': 'application/json',
  };
}

// One API call: tags the request with its endpoint group, records latency (unless 429), counts 429s and
// unexpected statuses. `expected` lists the statuses that are a normal outcome for this call.
function call(session, method, path, group, { body = null, expected = [200] } = {}) {
  const params = {
    headers: session ? authHeaders(session) : { Accept: 'application/json', 'Content-Type': 'application/json' },
    tags: { ep: group, name: group },
    responseCallback: http.expectedStatuses(...expected, 429),
    jar: new http.CookieJar(), // never let one VU's cookie jar leak between shared sessions
  };
  const payload = body === null ? null : JSON.stringify(body);
  const res = http.request(method, `${API}${path}`, payload, params);
  if (res.status === 429) {
    rateLimited.add(1, { ep: group });
    rateLimitedByGroup[group].add(1);
  } else {
    latency[group].add(res.timings.duration);
  }
  const ok = res.status === 429 || expected.includes(res.status);
  unexpected.add(!ok, { ep: group });
  if (!ok && __ENV.DEBUG) console.warn(`${method} ${path} -> ${res.status}: ${String(res.body).slice(0, 200)}`);
  if (res.status === 409) conflicts.add(1, { ep: group });
  return res;
}

function json(res) {
  try {
    return res.json();
  } catch (_) {
    return null;
  }
}

function pick(items) {
  return items[Math.floor(Math.random() * items.length)];
}

function weighted(entries) {
  const total = entries.reduce((sum, [, w]) => sum + w, 0);
  let r = Math.random() * total;
  for (const [value, w] of entries) {
    r -= w;
    if (r <= 0) return value;
  }
  return entries[entries.length - 1][0];
}

// Demo sign-in (public route, no cookies). Returns null when rate limited (20/min per client IP).
function login(persona) {
  const res = call(null, 'POST', '/auth/demo-login', 'login', { body: { persona } });
  if (res.status !== 200) return null;
  return { persona, session: res.cookies.nuvexa_session[0].value, csrf: res.cookies.nuvexa_csrf[0].value };
}

function waitFor(predicate, timeoutSeconds, interval = 0.5) {
  const deadline = Date.now() + timeoutSeconds * 1000;
  while (Date.now() < deadline) {
    const value = predicate();
    if (value) return value;
    sleep(interval);
  }
  return null;
}

// ------------------------------------------------------------------------------------------------ setup

export function setup() {
  const sessions = {};
  for (const persona of PERSONAS) {
    sessions[persona] = [];
    for (let i = 0; i < SESSIONS_PER_PERSONA; i += 1) {
      const s = login(persona);
      if (!s) throw new Error(`demo-login as ${persona} failed (rate limited or demo login disabled)`);
      sessions[persona].push(s);
    }
  }
  // Every persona with a demo mailbox syncs it once, so each has its own emails to analyze.
  for (const persona of MAILBOX_PERSONAS) {
    const s = sessions[persona][0];
    call(s, 'POST', '/emails/sync', 'sync', { body: {}, expected: [202] });
    waitFor(() => (json(call(s, 'GET', '/emails?limit=1', 'inbox_list')) || {}).total >= 10, 60);
  }
  return { sessions };
}

function sessionFor(data, persona) {
  return pick(data.sessions[persona]);
}

// ------------------------------------------------------------------------------------------------ browse

const PAGES = [
  ['overview', 20], ['inbox', 20], ['search', 10], ['email', 12], ['workflows', 8], ['workflow', 6],
  ['approvals', 10], ['action', 8], ['audit', 4], ['tasks', 2],
];

export function browse(data) {
  const persona = weighted([['member', 4], ['approver', 3], ['admin', 2], ['viewer', 1]]);
  const s = sessionFor(data, persona);
  const page = weighted(PAGES);
  // The SPA asks who is signed in on every full page load (react-query then caches it).
  if (Math.random() < 0.3) call(s, 'GET', '/auth/me', 'auth_me');

  switch (page) {
    case 'overview':
      call(s, 'GET', '/dashboard/summary', 'dashboard');
      call(s, 'GET', '/actions?status=pending_approval&status=uncertain&limit=5', 'actions_list');
      break;
    case 'inbox':
      call(s, 'GET', `/emails?limit=25&offset=${pick([0, 0, 0, 25, 50])}`, 'inbox_list');
      break;
    case 'search':
      call(s, 'GET', `/emails?limit=25&q=${encodeURIComponent(pick(SEARCH_TERMS))}`, 'inbox_search');
      break;
    case 'email': {
      const list = json(call(s, 'GET', '/emails?limit=25', 'inbox_list'));
      const items = (list && list.items) || [];
      if (items.length) call(s, 'GET', `/emails/${pick(items).id}`, 'email_detail');
      break;
    }
    case 'workflows':
      call(s, 'GET', '/workflows?limit=25', 'workflows_list');
      break;
    case 'workflow': {
      const runs = json(call(s, 'GET', '/workflows?limit=25', 'workflows_list'));
      const items = (runs && runs.items) || [];
      if (items.length) call(s, 'GET', `/workflows/${pick(items).id}`, 'workflow_detail');
      break;
    }
    case 'approvals':
      call(s, 'GET', '/actions?status=pending_approval&status=uncertain&limit=25', 'actions_list');
      break;
    case 'action': {
      const actions = json(call(s, 'GET', '/actions?limit=25', 'actions_list'));
      const items = (actions && actions.items) || [];
      if (items.length) {
        const id = pick(items).id;
        call(s, 'GET', `/actions/${id}`, 'action_detail');
        call(s, 'GET', `/actions/${id}/history`, 'action_history');
      }
      break;
    }
    case 'audit':
      call(s, 'GET', '/audit-events?limit=25', 'audit_log');
      break;
    default:
      call(s, 'GET', '/tasks?limit=25', 'tasks');
  }

  // Occasional user actions: "Sync now" (rate limited per user) and a fresh sign-in / sign-out.
  if (persona !== 'viewer' && Math.random() < 0.03) {
    call(s, 'POST', '/emails/sync', 'sync', { body: {}, expected: [202] });
  }
  if (Math.random() < 0.01) {
    const fresh = login(persona);
    if (fresh) call(fresh, 'POST', '/auth/logout', 'logout', { expected: [204] });
  }
  sleep(1 + Math.random() * 2); // think time
}

// ------------------------------------------------------------------------------------------------ flows

export function flow(data) {
  const owner = sessionFor(data, pick(MAILBOX_PERSONAS));
  const reviewer = sessionFor(data, pick(['approver', 'admin']));

  // 1. Analyze one of the owner's emails that has not been analyzed yet.
  const list = json(call(owner, 'GET', '/emails?analysis_status=not_analyzed&limit=50', 'inbox_list'));
  const candidates = (list && list.items) || [];
  if (!candidates.length) {
    flowsWithoutProposal.add(1, { reason: 'no_unanalyzed_email' });
    return;
  }
  const email = pick(candidates);
  const started = Date.now();
  const res = call(owner, 'POST', `/emails/${email.id}/analyze`, 'analyze', { expected: [200, 202, 409] });
  const body = json(res);
  if (!body || !body.workflow_run) return;
  const runId = body.workflow_run.id;
  const detail = waitFor(() => {
    const d = json(call(owner, 'GET', `/workflows/${runId}`, 'workflow_detail'));
    return d && !ANALYSIS_IN_FLIGHT.has(d.run.status) ? d : null;
  }, 60);
  if (!detail) {
    flowsWithoutProposal.add(1, { reason: 'analysis_timeout' });
    return;
  }
  flowAnalyzeSeconds.add((Date.now() - started) / 1000);

  // 2. The reviewer opens the proposal and approves exactly the version and payload they saw.
  const proposals = detail.actions.filter((a) => a.status === 'pending_approval');
  const action =
    proposals.find((a) => a.action_type === 'create_azure_devops_work_item') || proposals.find(() => true);
  if (!action) {
    flowsWithoutProposal.add(1, { reason: 'no_pending_proposal' });
    return;
  }
  const current = json(call(reviewer, 'GET', `/actions/${action.id}`, 'action_detail'));
  if (!current) return;
  const approved = call(reviewer, 'POST', `/actions/${action.id}/approve`, 'approve', {
    body: { version: current.version, payload_hash: current.payload_hash, comment: 'k6 load smoke' },
    expected: [200, 409],
  });
  if (approved.status !== 200) return;

  // 3. Execute (queued job) and wait for the outcome.
  const executeStarted = Date.now();
  const executed = call(reviewer, 'POST', `/actions/${action.id}/execute`, 'execute', {
    body: { version: current.version, payload_hash: current.payload_hash },
    expected: [202, 409],
  });
  if (executed.status !== 202) return;
  const outcome = waitFor(() => {
    const a = json(call(reviewer, 'GET', `/actions/${action.id}`, 'action_detail'));
    return a && ACTION_TERMINAL.has(a.status) ? a : null;
  }, 60);
  if (outcome) {
    flowExecuteSeconds.add((Date.now() - executeStarted) / 1000);
    if (outcome.status !== 'completed') executionOutcomes.add(1, { status: outcome.status });
    flowsCompleted.add(1);
  }
}

// ------------------------------------------------------------------------------------------------ summary

function fmt(value) {
  return value === undefined || value === null ? '-' : Number(value).toFixed(1);
}

export function handleSummary(data) {
  const m = data.metrics;
  const lines = [];
  lines.push('endpoint group          count     p50(ms)   p95(ms)   p99(ms)   max(ms)   429s');
  for (const g of GROUPS) {
    const t = m[`latency_${g}`];
    if (!t || !t.values || !t.values.count) continue;
    const v = t.values;
    const limited = (m[`rate_limited_${g}`] && m[`rate_limited_${g}`].values.count) || 0;
    lines.push(
      `${g.padEnd(22)}${String(v.count).padStart(7)}${fmt(v.med).padStart(12)}${fmt(v['p(95)']).padStart(10)}` +
        `${fmt(v['p(99)']).padStart(10)}${fmt(v.max).padStart(10)}${String(limited).padStart(7)}`,
    );
  }
  const all = m.http_req_duration ? m.http_req_duration.values : {};
  lines.push(
    `${'all requests'.padEnd(22)}${String(all.count || 0).padStart(7)}${fmt(all.med).padStart(12)}` +
      `${fmt(all['p(95)']).padStart(10)}${fmt(all['p(99)']).padStart(10)}${fmt(all.max).padStart(10)}`,
  );
  const reqs = m.http_reqs ? m.http_reqs.values : {};
  const unexpectedRate = m.unexpected_responses ? m.unexpected_responses.values.rate : 0;
  lines.push('');
  lines.push(`requests ${reqs.count} (${fmt(reqs.rate)}/s), unexpected responses ${(unexpectedRate * 100).toFixed(3)}%,`);
  lines.push(`429 rate limited ${(m.rate_limited && m.rate_limited.values.count) || 0}, 409 conflicts ${(m.flow_conflicts && m.flow_conflicts.values.count) || 0}`);
  for (const name of ['flow_analyze_seconds', 'flow_execute_seconds']) {
    const t = m[name];
    if (t && t.values && t.values.count) {
      lines.push(`${name}: n=${t.values.count} p50=${fmt(t.values.med)}s p95=${fmt(t.values['p(95)'])}s max=${fmt(t.values.max)}s`);
    }
  }
  lines.push(`flows completed ${(m.flows_completed && m.flows_completed.values.count) || 0}, without proposal ${(m.flows_without_proposal && m.flows_without_proposal.values.count) || 0}, executions not completed ${(m.execution_outcome_not_completed && m.execution_outcome_not_completed.values.count) || 0}`);
  const failed = Object.entries(m)
    .filter(([, metric]) => metric.thresholds && Object.values(metric.thresholds).some((t) => !t.ok))
    .map(([name]) => name);
  lines.push(`thresholds: ${failed.length ? `FAILED ${failed.join(', ')}` : 'all passed'}`);
  return { stdout: `${lines.join('\n')}\n`, [SUMMARY_PATH]: JSON.stringify(data, null, 1) };
}
