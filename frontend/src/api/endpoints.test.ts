import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { errorResponse, jsonResponse, mockFetch, queryOf, type RecordedCall } from '../test/utils';
import { ApiError, CSRF_HEADER, REQUEST_ID_HEADER, setUnauthorizedHandler } from './client';
import {
  actionsApi,
  auditApi,
  authApi,
  dashboardApi,
  emailsApi,
  healthApi,
  integrationsApi,
  replyDraftsApi,
  settingsApi,
  tasksApi,
  workflowsApi,
} from './endpoints';
import type { SettingsUpdateRequest } from './types';

const CSRF = 'csrf-endpoints-token';

function clearCookies() {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

const NO_CONTENT = Symbol('204');

/** Capture every request, answering with `status` and `body` (204 for NO_CONTENT). */
function captureAll(body: unknown, status = 200) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const raw = typeof init?.body === 'string' ? init.body : undefined;
    calls.push({
      method: (init?.method ?? 'GET').toUpperCase(),
      url,
      headers: new Headers(init?.headers),
      body: raw ? (JSON.parse(raw) as unknown) : undefined,
    });
    return Promise.resolve(
      body === NO_CONTENT ? new Response(null, { status: 204 }) : jsonResponse(body, status),
    );
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls, fetchMock };
}

function only(calls: RecordedCall[]): RecordedCall {
  expect(calls).toHaveLength(1);
  return calls[0]!;
}

function pathOf(call: RecordedCall): string {
  return call.url.split('?')[0]!;
}

function expectSafe(call: RecordedCall) {
  expect(call.method).toBe('GET');
  expect(call.headers.get(CSRF_HEADER)).toBeNull();
  expect(call.headers.get(REQUEST_ID_HEADER)).toBeTruthy();
  expect(call.body).toBeUndefined();
}

function expectUnsafe(call: RecordedCall, method: string) {
  expect(call.method).toBe(method);
  expect(call.headers.get(CSRF_HEADER)).toBe(CSRF);
  expect(call.headers.get(REQUEST_ID_HEADER)).toBeTruthy();
}

beforeEach(() => {
  clearCookies();
  document.cookie = `nuvexa_csrf=${CSRF}; path=/`;
});

afterEach(() => {
  clearCookies();
  vi.unstubAllGlobals();
});

describe('healthApi', () => {
  it.each([
    ['live', () => healthApi.live(), '/api/v1/health/live'],
    ['ready', () => healthApi.ready(), '/api/v1/health/ready'],
  ] as const)('%s → GET %s', async (_name, call, url) => {
    const { calls } = captureAll({ status: 'ok', version: '1', checks: {} });
    await expect(call()).resolves.toEqual({ status: 'ok', version: '1', checks: {} });
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe(url);
  });
});

describe('authApi', () => {
  it('config → GET /auth/config', async () => {
    const { calls } = captureAll({ app_name: 'NUVEXA' });
    await authApi.config();
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/auth/config');
  });

  it('me → GET /auth/me', async () => {
    const { calls } = captureAll({ user: {}, permissions: [] });
    await authApi.me();
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/auth/me');
  });

  it('demoLogin → POST /auth/demo-login with the persona and CSRF', async () => {
    const { calls } = captureAll({ user: {}, permissions: [] });
    await authApi.demoLogin('viewer');
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe('/api/v1/auth/demo-login');
    expect(c.body).toEqual({ persona: 'viewer' });
    expect(c.headers.get('Content-Type')).toBe('application/json');
  });

  it('logout → POST /auth/logout with no body (204 → undefined)', async () => {
    const { calls } = captureAll(NO_CONTENT);
    await expect(authApi.logout()).resolves.toBeUndefined();
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe('/api/v1/auth/logout');
    expect(c.body).toBeUndefined();
    expect(c.headers.get('Content-Type')).toBeNull();
  });
});

describe('401 handling per endpoint group (skipAuthRedirect)', () => {
  const noRedirect: [string, () => Promise<unknown>][] = [
    ['healthApi.live', () => healthApi.live()],
    ['healthApi.ready', () => healthApi.ready()],
    ['authApi.config', () => authApi.config()],
    ['authApi.me', () => authApi.me()],
    ['authApi.demoLogin', () => authApi.demoLogin('admin')],
    ['authApi.logout', () => authApi.logout()],
  ];
  const redirect: [string, () => Promise<unknown>][] = [
    ['integrationsApi.get', () => integrationsApi.get()],
    ['emailsApi.list', () => emailsApi.list({})],
    ['workflowsApi.get', () => workflowsApi.get('run-1')],
    ['actionsApi.approve', () => actionsApi.approve('a', { version: 1, payload_hash: 'h' })],
    ['tasksApi.list', () => tasksApi.list({})],
    ['replyDraftsApi.list', () => replyDraftsApi.list({})],
    ['auditApi.list', () => auditApi.list({})],
    ['settingsApi.get', () => settingsApi.get()],
    ['dashboardApi.summary', () => dashboardApi.summary()],
  ];

  function mock401() {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(errorResponse(401, 'authentication_required', { correlationId: null }))),
    );
  }

  it.each(noRedirect)('%s does not invoke the unauthorized handler on 401', async (_name, call) => {
    const handler = vi.fn();
    const restore = setUnauthorizedHandler(handler);
    mock401();
    const error = await call().catch((e: unknown) => e);
    restore();
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).isUnauthorized).toBe(true);
    expect(handler).not.toHaveBeenCalled();
  });

  it.each(redirect)('%s invokes the unauthorized handler on 401', async (_name, call) => {
    const handler = vi.fn();
    const restore = setUnauthorizedHandler(handler);
    mock401();
    const error = await call().catch((e: unknown) => e);
    restore();
    expect(error).toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

describe('integrationsApi', () => {
  it('get → GET /integrations', async () => {
    const { calls } = captureAll({});
    await integrationsApi.get();
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/integrations');
  });

  it('disconnect → DELETE /integrations/connections/{id} (id encoded) with CSRF and no body', async () => {
    const { calls } = captureAll(NO_CONTENT);
    await integrationsApi.disconnect('conn/1 x');
    const c = only(calls);
    expectUnsafe(c, 'DELETE');
    expect(c.url).toBe('/api/v1/integrations/connections/conn%2F1%20x');
    expect(c.body).toBeUndefined();
  });

  it('testAzureDevOps → GET /integrations/azure-devops/test', async () => {
    const { calls } = captureAll({ ok: true });
    await integrationsApi.testAzureDevOps();
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/integrations/azure-devops/test');
  });

  it('adoProjects → GET /integrations/azure-devops/projects', async () => {
    const { calls } = captureAll({ items: [] });
    await integrationsApi.adoProjects();
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/integrations/azure-devops/projects');
  });

  it('adoWorkItemTypes → GET …/projects/{project}/work-item-types with the project name encoded', async () => {
    const { calls } = captureAll({ project: 'Pay Ments', items: [] });
    await integrationsApi.adoWorkItemTypes('Pay Ments/EU');
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/integrations/azure-devops/projects/Pay%20Ments%2FEU/work-item-types');
  });

  it('validateAdoPayload → POST /integrations/azure-devops/validate with the body', async () => {
    const { calls } = captureAll({ valid: true });
    const body = { payload: { kind: 'create_azure_devops_work_item', title: 'x' }, server_side: true };
    await integrationsApi.validateAdoPayload(body);
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe('/api/v1/integrations/azure-devops/validate');
    expect(c.body).toEqual(body);
  });

  it('maps an error response to ApiError (403 forbidden)', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/integrations',
        handler: () => errorResponse(403, 'permission_denied', { message: 'Nope', correlationId: 'c-403' }),
      },
    ]);
    const error = (await integrationsApi.get().catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.isForbidden).toBe(true);
    expect(error.code).toBe('permission_denied');
    expect(error.supportReference).toBe('c-403');
  });
});

describe('emailsApi', () => {
  it('list → GET /emails with every filter as a query param and undefined filters omitted', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 25, offset: 0 });
    await emailsApi.list({
      q: 'checkout outage',
      category: 'incident',
      priority: 'high',
      analysis_status: 'awaiting_review',
      connected_account_id: 'conn-1',
      limit: 25,
      offset: 50,
    });
    const c = only(calls);
    expectSafe(c);
    expect(pathOf(c)).toBe('/api/v1/emails');
    expect(Object.fromEntries(queryOf(c))).toEqual({
      q: 'checkout outage',
      category: 'incident',
      priority: 'high',
      analysis_status: 'awaiting_review',
      connected_account_id: 'conn-1',
      limit: '25',
      offset: '50',
    });
  });

  it('list with no filters → no query string', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 25, offset: 0 });
    await emailsApi.list({ q: undefined, category: undefined });
    expect(only(calls).url).toBe('/api/v1/emails');
  });

  it('get → GET /emails/{id} (encoded)', async () => {
    const { calls } = captureAll({});
    await emailsApi.get('e 1/2');
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/emails/e%201%2F2');
  });

  it('sync → POST /emails/sync with {} by default', async () => {
    const { calls } = captureAll({ job_ids: [], message: 'ok' });
    await emailsApi.sync();
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe('/api/v1/emails/sync');
    expect(c.body).toEqual({});
  });

  it('sync → POST /emails/sync with the connected account', async () => {
    const { calls } = captureAll({ job_ids: ['j'], message: 'ok' });
    await emailsApi.sync({ connected_account_id: 'conn-1' });
    expect(only(calls).body).toEqual({ connected_account_id: 'conn-1' });
  });

  it('analyze → POST /emails/{id}/analyze with no body', async () => {
    const { calls } = captureAll({ created: true });
    await emailsApi.analyze('email-1');
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe('/api/v1/emails/email-1/analyze');
    expect(c.body).toBeUndefined();
  });

  it('maps a 404 to ApiError.isNotFound', async () => {
    mockFetch([
      { method: 'GET', path: '/api/v1/emails/missing', handler: () => errorResponse(404, 'not_found') },
    ]);
    const error = (await emailsApi.get('missing').catch((e: unknown) => e)) as ApiError;
    expect(error.isNotFound).toBe(true);
    expect(error.code).toBe('not_found');
  });
});

describe('workflowsApi', () => {
  it('list → GET /workflows with status/email_id/limit/offset', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 25, offset: 0 });
    await workflowsApi.list({ status: 'failed', email_id: 'email-1', limit: 25, offset: 0 });
    const c = only(calls);
    expectSafe(c);
    expect(pathOf(c)).toBe('/api/v1/workflows');
    expect(Object.fromEntries(queryOf(c))).toEqual({
      status: 'failed',
      email_id: 'email-1',
      limit: '25',
      offset: '0',
    });
  });

  it('list omits undefined params', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 25, offset: 0 });
    await workflowsApi.list({ status: undefined, limit: 10 });
    expect(only(calls).url).toBe('/api/v1/workflows?limit=10');
  });

  it.each([
    ['get', (id: string) => workflowsApi.get(id), ''],
    ['analysis', (id: string) => workflowsApi.analysis(id), '/analysis'],
    ['actions', (id: string) => workflowsApi.actions(id), '/actions'],
    ['events', (id: string) => workflowsApi.events(id), '/events'],
  ] as const)('%s → GET /workflows/{id}%s', async (_name, call, suffix) => {
    const { calls } = captureAll({});
    await call('run/1');
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe(`/api/v1/workflows/run%2F1${suffix}`);
  });

  it.each([
    ['reanalyze', (id: string) => workflowsApi.reanalyze(id), '/reanalyze'],
    ['cancel', (id: string) => workflowsApi.cancel(id), '/cancel'],
  ] as const)('%s → POST /workflows/{id}%s with CSRF and no body', async (_name, call, suffix) => {
    const { calls } = captureAll({});
    await call('run-1');
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe(`/api/v1/workflows/run-1${suffix}`);
    expect(c.body).toBeUndefined();
  });

  it('maps a 409 conflict on cancel', async () => {
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/workflows/run-1/cancel',
        handler: () => errorResponse(409, 'invalid_state', { message: 'Already completed' }),
      },
    ]);
    const error = (await workflowsApi.cancel('run-1').catch((e: unknown) => e)) as ApiError;
    expect(error.isConflict).toBe(true);
    expect(error.isStaleVersion).toBe(false);
    expect(error.message).toBe('Already completed');
  });
});

describe('actionsApi', () => {
  it('list sends one status as a single param', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 1, offset: 0 });
    await actionsApi.list({ status: 'pending_approval', limit: 1, offset: 0 });
    const c = only(calls);
    expectSafe(c);
    expect(pathOf(c)).toBe('/api/v1/actions');
    expect(queryOf(c).getAll('status')).toEqual(['pending_approval']);
    expect(queryOf(c).get('limit')).toBe('1');
    expect(queryOf(c).get('offset')).toBe('0');
  });

  it('list sends several statuses as repeated `status` params, plus type and run filters', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 25, offset: 0 });
    await actionsApi.list({
      status: ['failed', 'uncertain'],
      action_type: 'create_azure_devops_work_item',
      workflow_run_id: 'run-1',
      limit: 25,
      offset: 25,
    });
    const c = only(calls);
    expect(c.url).toBe(
      '/api/v1/actions?status=failed&status=uncertain&action_type=create_azure_devops_work_item&workflow_run_id=run-1&limit=25&offset=25',
    );
  });

  it('list omits undefined filters', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 25, offset: 0 });
    await actionsApi.list({ status: undefined, action_type: undefined });
    expect(only(calls).url).toBe('/api/v1/actions');
  });

  it.each([
    ['get', (id: string) => actionsApi.get(id), ''],
    ['executions', (id: string) => actionsApi.executions(id), '/executions'],
    ['history', (id: string) => actionsApi.history(id), '/history'],
  ] as const)('%s → GET /actions/{id}%s', async (_name, call, suffix) => {
    const { calls } = captureAll({});
    await call('act 1');
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe(`/api/v1/actions/act%201${suffix}`);
  });

  it('update → PATCH /actions/{id} with expected_version + payload', async () => {
    const { calls } = captureAll({});
    const body = { expected_version: 3, payload: { kind: 'mark_no_action', reason: 'FYI' } };
    await actionsApi.update('act-1', body);
    const c = only(calls);
    expectUnsafe(c, 'PATCH');
    expect(c.url).toBe('/api/v1/actions/act-1');
    expect(c.body).toEqual(body);
  });

  it('approve → POST /actions/{id}/approve with version + payload_hash + comment', async () => {
    const { calls } = captureAll({});
    await actionsApi.approve('act-1', { version: 3, payload_hash: 'a'.repeat(64), comment: 'ok' });
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe('/api/v1/actions/act-1/approve');
    expect(c.body).toEqual({ version: 3, payload_hash: 'a'.repeat(64), comment: 'ok' });
  });

  it('reject → POST /actions/{id}/reject with the reason', async () => {
    const { calls } = captureAll({});
    await actionsApi.reject('act-1', { reason: 'Duplicate' });
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe('/api/v1/actions/act-1/reject');
    expect(c.body).toEqual({ reason: 'Duplicate' });
  });

  it('cancel → POST /actions/{id}/cancel with a nullable reason', async () => {
    const { calls } = captureAll({});
    await actionsApi.cancel('act-1', { reason: null });
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe('/api/v1/actions/act-1/cancel');
    expect(c.body).toEqual({ reason: null });
  });

  it('execute → POST /actions/{id}/execute with version + payload_hash only', async () => {
    const { calls } = captureAll({ action: {}, job_id: 'j' }, 202);
    await expect(actionsApi.execute('act-1', { version: 4, payload_hash: 'b'.repeat(64) })).resolves.toEqual({
      action: {},
      job_id: 'j',
    });
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe('/api/v1/actions/act-1/execute');
    expect(c.body).toEqual({ version: 4, payload_hash: 'b'.repeat(64) });
  });

  it('reconcile → POST /actions/{id}/reconcile with no body', async () => {
    const { calls } = captureAll({ action: {}, job_id: 'j' }, 202);
    await actionsApi.reconcile('act-1');
    const c = only(calls);
    expectUnsafe(c, 'POST');
    expect(c.url).toBe('/api/v1/actions/act-1/reconcile');
    expect(c.body).toBeUndefined();
    expect(c.headers.get('Content-Type')).toBeNull();
  });

  it('maps 409 stale_version and 422 field errors', async () => {
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/actions/act-1/approve',
        handler: () => errorResponse(409, 'stale_version', { correlationId: 'corr-stale' }),
      },
      {
        method: 'PATCH',
        path: '/api/v1/actions/act-1',
        handler: () =>
          errorResponse(422, 'validation_failed', {
            details: [{ loc: ['body', 'payload', 'title'], msg: 'Too long' }],
          }),
      },
    ]);
    const stale = (await actionsApi
      .approve('act-1', { version: 3, payload_hash: 'h' })
      .catch((e: unknown) => e)) as ApiError;
    expect(stale.isStaleVersion).toBe(true);
    expect(stale.correlationId).toBe('corr-stale');
    const invalid = (await actionsApi
      .update('act-1', { expected_version: 3, payload: {} })
      .catch((e: unknown) => e)) as ApiError;
    expect(invalid.isValidation).toBe(true);
    expect(invalid.fieldErrors).toEqual([{ field: 'title', message: 'Too long' }]);
  });
});

describe('tasksApi', () => {
  it('list → GET /tasks with status/limit/offset', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 25, offset: 0 });
    await tasksApi.list({ status: 'open', limit: 25, offset: 0 });
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/tasks?status=open&limit=25&offset=0');
  });

  it('list omits an undefined status', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 25, offset: 0 });
    await tasksApi.list({ status: undefined, limit: 25 });
    expect(only(calls).url).toBe('/api/v1/tasks?limit=25');
  });

  it('update → PATCH /tasks/{id} with {status}', async () => {
    const { calls } = captureAll({});
    await tasksApi.update('task/1', 'done');
    const c = only(calls);
    expectUnsafe(c, 'PATCH');
    expect(c.url).toBe('/api/v1/tasks/task%2F1');
    expect(c.body).toEqual({ status: 'done' });
  });

  it('maps a network failure to status 0', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const error = (await tasksApi.update('t', 'done').catch((e: unknown) => e)) as ApiError;
    expect(error.isNetworkError).toBe(true);
  });
});

describe('replyDraftsApi', () => {
  it('list → GET /reply-drafts with paging', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 25, offset: 0 });
    await replyDraftsApi.list({ limit: 25, offset: 25 });
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/reply-drafts?limit=25&offset=25');
  });

  it('maps a 500 to ApiError', async () => {
    mockFetch([
      { method: 'GET', path: '/api/v1/reply-drafts', handler: () => errorResponse(500, 'internal') },
    ]);
    const error = (await replyDraftsApi.list({}).catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(500);
    expect(error.code).toBe('internal');
  });
});

describe('auditApi', () => {
  it('list → GET /audit-events with every filter', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 50, offset: 0 });
    await auditApi.list({
      event_type: 'action.approved',
      resource_type: 'action',
      resource_id: 'act-1',
      action_id: 'act-1',
      outcome: 'denied',
      limit: 50,
      offset: 100,
    });
    const c = only(calls);
    expectSafe(c);
    expect(pathOf(c)).toBe('/api/v1/audit-events');
    expect(Object.fromEntries(queryOf(c))).toEqual({
      event_type: 'action.approved',
      resource_type: 'action',
      resource_id: 'act-1',
      action_id: 'act-1',
      outcome: 'denied',
      limit: '50',
      offset: '100',
    });
  });

  it('list omits undefined filters', async () => {
    const { calls } = captureAll({ items: [], total: 0, limit: 50, offset: 0 });
    await auditApi.list({ event_type: undefined, outcome: undefined, limit: 50 });
    expect(only(calls).url).toBe('/api/v1/audit-events?limit=50');
  });

  it('maps a 403 to ApiError', async () => {
    mockFetch([
      { method: 'GET', path: '/api/v1/audit-events', handler: () => errorResponse(403, 'forbidden') },
    ]);
    const error = (await auditApi.list({}).catch((e: unknown) => e)) as ApiError;
    expect(error.isForbidden).toBe(true);
  });
});

describe('settingsApi', () => {
  it('get → GET /settings', async () => {
    const { calls } = captureAll({});
    await settingsApi.get();
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/settings');
  });

  it('update → PUT /settings with expected_version + settings and CSRF', async () => {
    const { calls } = captureAll({});
    const body = { expected_version: 2, settings: { auto_analyze_new_emails: true } };
    await settingsApi.update(body as unknown as SettingsUpdateRequest);
    const c = only(calls);
    expectUnsafe(c, 'PUT');
    expect(c.url).toBe('/api/v1/settings');
    expect(c.body).toEqual(body);
  });

  it('maps 409 stale_version on update', async () => {
    mockFetch([
      { method: 'PUT', path: '/api/v1/settings', handler: () => errorResponse(409, 'stale_version') },
    ]);
    const error = (await settingsApi
      .update({ expected_version: 1 } as unknown as SettingsUpdateRequest)
      .catch((e: unknown) => e)) as ApiError;
    expect(error.isStaleVersion).toBe(true);
  });
});

describe('dashboardApi', () => {
  it('summary → GET /dashboard/summary', async () => {
    const { calls } = captureAll({ emails_total: 1 });
    await expect(dashboardApi.summary()).resolves.toEqual({ emails_total: 1 });
    const c = only(calls);
    expectSafe(c);
    expect(c.url).toBe('/api/v1/dashboard/summary');
  });

  it('maps a 503 to ApiError', async () => {
    mockFetch([
      { method: 'GET', path: '/api/v1/dashboard/summary', handler: () => errorResponse(503, 'unavailable') },
    ]);
    const error = (await dashboardApi.summary().catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(503);
  });
});

describe('request plumbing shared by every endpoint', () => {
  it('always uses same-origin credentials and Accept: application/json', async () => {
    const { fetchMock } = captureAll({});
    await dashboardApi.summary();
    await actionsApi.reject('a', { reason: 'r' });
    for (const [, init] of fetchMock.mock.calls) {
      expect(init?.credentials).toBe('same-origin');
      expect(new Headers(init?.headers).get('Accept')).toBe('application/json');
    }
  });

  it('omits the CSRF header when the cookie is missing', async () => {
    clearCookies();
    const { calls } = captureAll({});
    await actionsApi.reject('a', { reason: 'r' });
    expect(only(calls).headers.get(CSRF_HEADER)).toBeNull();
  });
});
