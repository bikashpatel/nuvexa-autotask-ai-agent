import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { errorResponse, jsonResponse, mockFetch } from '../test/utils';
import {
  API_BASE,
  ApiError,
  CSRF_COOKIE,
  CSRF_HEADER,
  REQUEST_ID_HEADER,
  apiRequest,
  buildQuery,
  generateRequestId,
  http,
  isApiError,
  navigationUrl,
  parseFieldErrors,
  readCookie,
  safeRedirectPath,
  setUnauthorizedHandler,
} from './client';

function clearCookies() {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

describe('apiRequest', () => {
  beforeEach(() => {
    clearCookies();
    document.cookie = 'nuvexa_csrf=csrf-token-123; path=/';
  });
  afterEach(() => {
    clearCookies();
    vi.unstubAllGlobals();
  });

  it('does not send the CSRF header on GET, but always sends a request id and same-origin credentials', async () => {
    const { fetchMock, calls } = mockFetch([
      {
        method: 'GET',
        path: '/api/v1/emails',
        handler: () => jsonResponse({ items: [], total: 0, limit: 25, offset: 0 }),
      },
    ]);
    await apiRequest('/emails', { query: { q: 'outage', category: undefined } });
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe('/api/v1/emails?q=outage');
    expect(calls[0]!.headers.get(CSRF_HEADER)).toBeNull();
    expect(calls[0]!.headers.get(REQUEST_ID_HEADER)).toMatch(/^[0-9a-f-]{16,}$/i);
    const init = fetchMock.mock.calls[0]![1];
    expect(init?.credentials).toBe('same-origin');
  });

  it.each(['POST', 'PUT', 'PATCH', 'DELETE'] as const)(
    'sends X-CSRF-Token from the nuvexa_csrf cookie on %s',
    async (method) => {
      const { calls } = mockFetch([
        { method, path: '/api/v1/things', handler: () => new Response(null, { status: 204 }) },
      ]);
      await apiRequest('/things', { method, body: method === 'DELETE' ? undefined : { a: 1 } });
      expect(calls[0]!.headers.get(CSRF_HEADER)).toBe('csrf-token-123');
      if (method !== 'DELETE') {
        expect(calls[0]!.headers.get('Content-Type')).toBe('application/json');
        expect(calls[0]!.body).toEqual({ a: 1 });
      }
    },
  );

  it('generates a distinct request id per request', async () => {
    const { calls } = mockFetch([{ method: 'GET', path: '/api/v1/x', handler: () => jsonResponse({}) }]);
    await apiRequest('/x');
    await apiRequest('/x');
    expect(calls[0]!.headers.get(REQUEST_ID_HEADER)).not.toBe(calls[1]!.headers.get(REQUEST_ID_HEADER));
  });

  it('parses the API error schema into an ApiError', async () => {
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/actions/a1/approve',
        handler: () =>
          jsonResponse(
            {
              error: {
                code: 'stale_version',
                message: 'The resource was modified. Reload and review the latest version.',
                correlation_id: 'corr-42',
                details: [],
              },
            },
            409,
            { 'X-Request-ID': 'echoed-id' },
          ),
      },
    ]);
    const error = await apiRequest('/actions/a1/approve', { method: 'POST', body: {} }).catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(409);
    expect(apiError.code).toBe('stale_version');
    expect(apiError.isStaleVersion).toBe(true);
    expect(apiError.correlationId).toBe('corr-42');
    expect(apiError.requestId).toBe('echoed-id');
    expect(apiError.supportReference).toBe('corr-42');
    expect(apiError.message).toMatch(/modified/);
  });

  it('exposes 422 field errors in both {field,message} and FastAPI {loc,msg} shapes', async () => {
    mockFetch([
      {
        method: 'PATCH',
        path: '/api/v1/actions/a1',
        handler: () =>
          jsonResponse(
            {
              error: {
                code: 'validation_failed',
                message: 'The request failed validation.',
                correlation_id: 'c',
                details: [
                  { loc: ['body', 'payload', 'title'], msg: 'String should have at most 255 characters' },
                  { field: 'payload.priority', message: 'Input should be less than or equal to 4' },
                ],
              },
            },
            422,
          ),
      },
    ]);
    const error = (await apiRequest('/actions/a1', { method: 'PATCH', body: {} }).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(error.isValidation).toBe(true);
    expect(error.fieldErrors).toEqual([
      { field: 'title', message: 'String should have at most 255 characters' },
      { field: 'priority', message: 'Input should be less than or equal to 4' },
    ]);
  });

  it('falls back to a generic error for non-JSON failures', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/x',
        handler: () => new Response('Bad gateway', { status: 502, statusText: 'Bad Gateway' }),
      },
    ]);
    const error = (await apiRequest('/x').catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(502);
    expect(error.code).toBe('http_502');
    expect(error.requestId).toBeTruthy();
  });

  it('maps network failures to status 0 / network_error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const error = (await apiRequest('/x').catch((e: unknown) => e)) as ApiError;
    expect(error.isNetworkError).toBe(true);
    expect(error.code).toBe('network_error');
  });

  describe('401 handling', () => {
    const unauthorized = {
      error: {
        code: 'authentication_required',
        message: 'Authentication is required.',
        correlation_id: null,
        details: [],
      },
    };

    it('invokes the unauthorized handler (redirect to /login) and still rejects', async () => {
      const handler = vi.fn();
      const restore = setUnauthorizedHandler(handler);
      mockFetch([{ method: 'GET', path: '/api/v1/emails', handler: () => jsonResponse(unauthorized, 401) }]);
      const error = (await apiRequest('/emails').catch((e: unknown) => e)) as ApiError;
      restore();
      expect(handler).toHaveBeenCalledTimes(1);
      expect(error.isUnauthorized).toBe(true);
      expect(error.code).toBe('authentication_required');
    });

    it('does not redirect when skipAuthRedirect is set (session probe)', async () => {
      const handler = vi.fn();
      const restore = setUnauthorizedHandler(handler);
      mockFetch([{ method: 'GET', path: '/api/v1/auth/me', handler: () => jsonResponse(unauthorized, 401) }]);
      await expect(apiRequest('/auth/me', { skipAuthRedirect: true })).rejects.toBeInstanceOf(ApiError);
      restore();
      expect(handler).not.toHaveBeenCalled();
    });

    it('default handler navigates to /login with a redirect_to back to the current page', async () => {
      const assign = vi.fn();
      const original = window.location;
      Object.defineProperty(window, 'location', {
        configurable: true,
        value: { ...original, pathname: '/approvals', search: '?view=attention', assign },
      });
      mockFetch([{ method: 'GET', path: '/api/v1/actions', handler: () => jsonResponse(unauthorized, 401) }]);
      await apiRequest('/actions').catch(() => undefined);
      Object.defineProperty(window, 'location', { configurable: true, value: original });
      expect(assign).toHaveBeenCalledWith(
        `/login?redirect_to=${encodeURIComponent('/approvals?view=attention')}`,
      );
    });
  });
});

describe('helpers', () => {
  it('buildQuery skips empty values and repeats array values', () => {
    expect(buildQuery({ status: ['failed', 'uncertain'], q: '', limit: 25, x: null })).toBe(
      '?status=failed&status=uncertain&limit=25',
    );
    expect(buildQuery({})).toBe('');
  });

  it('safeRedirectPath only allows relative in-app paths', () => {
    expect(safeRedirectPath('/inbox?x=1')).toBe('/inbox?x=1');
    expect(safeRedirectPath('//evil.example')).toBe('/');
    expect(safeRedirectPath('https://evil.example')).toBe('/');
    expect(safeRedirectPath('/\\evil.example')).toBe('/');
    expect(safeRedirectPath('/login')).toBe('/');
    expect(safeRedirectPath(null)).toBe('/');
  });

  it('parseFieldErrors ignores entries without a message', () => {
    expect(parseFieldErrors([{ loc: ['body', 'reason'] }, { field: 'reason', message: 'required' }])).toEqual(
      [{ field: 'reason', message: 'required' }],
    );
  });
});

describe('readCookie', () => {
  afterEach(() => {
    clearCookies();
  });

  it('returns null when there are no cookies or the name is absent', () => {
    clearCookies();
    expect(readCookie('nuvexa_csrf')).toBeNull();
    document.cookie = 'other=1; path=/';
    expect(readCookie('nuvexa_csrf')).toBeNull();
  });

  it('decodes URI-encoded values and trims whitespace around parts', () => {
    document.cookie = 'first=1; path=/';
    document.cookie = `nuvexa_csrf=${encodeURIComponent('a b/c=d')}; path=/`;
    expect(readCookie('nuvexa_csrf')).toBe('a b/c=d');
    expect(readCookie('first')).toBe('1');
  });

  it('returns the raw value when it is not valid percent-encoding', () => {
    document.cookie = 'broken=%E0%A4%A; path=/';
    expect(readCookie('broken')).toBe('%E0%A4%A');
  });

  it('skips cookie parts without "=" and does not prefix-match names', () => {
    const spy = vi
      .spyOn(document, 'cookie', 'get')
      .mockReturnValue('flag; nuvexa_csrf_old=x; nuvexa_csrf=tok');
    expect(readCookie('nuvexa_csrf')).toBe('tok');
    expect(readCookie('flag')).toBeNull();
    spy.mockRestore();
  });
});

describe('generateRequestId', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses crypto.randomUUID when available', () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'uuid-from-crypto' });
    expect(generateRequestId()).toBe('uuid-from-crypto');
  });

  it('falls back to getRandomValues (32 hex chars) without randomUUID', () => {
    vi.stubGlobal('crypto', {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.forEach((_, i) => (bytes[i] = i));
        return bytes;
      },
    });
    expect(generateRequestId()).toBe('000102030405060708090a0b0c0d0e0f');
  });

  it('falls back to Math.random without any crypto object', () => {
    vi.stubGlobal('crypto', undefined);
    vi.spyOn(Math, 'random').mockReturnValue(0.999);
    expect(generateRequestId()).toBe('ff'.repeat(16));
  });
});

describe('buildQuery edge cases', () => {
  it('returns "" for undefined, all-empty and empty-array queries', () => {
    expect(buildQuery(undefined)).toBe('');
    expect(buildQuery({ a: undefined, b: null, c: '', d: [] })).toBe('');
  });

  it('serialises numbers, booleans and zero, encodes special characters, skips empty array items', () => {
    expect(buildQuery({ offset: 0, flag: false, q: 'a&b c', s: ['x', '', null, 'y'] })).toBe(
      '?offset=0&flag=false&q=a%26b+c&s=x&s=y',
    );
  });
});

describe('parseFieldErrors', () => {
  it('strips body/payload/settings prefixes, keeps numeric loc parts and empty paths', () => {
    expect(
      parseFieldErrors([
        { loc: ['body', 'settings', 'retention', 'audit_days'], msg: 'too small' },
        { loc: ['body', 'payload', 'tags', 3], msg: 'bad tag' },
        { field: 'payload.title', message: 'required' },
        { message: 'model-level' },
        { msg: 42 },
      ]),
    ).toEqual([
      { field: 'retention.audit_days', message: 'too small' },
      { field: 'tags.3', message: 'bad tag' },
      { field: 'title', message: 'required' },
      { field: '', message: 'model-level' },
    ]);
  });
});

describe('apiRequest response parsing', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([204, 205])('returns undefined for %s without reading a body', async (status) => {
    mockFetch([{ method: 'POST', path: '/api/v1/x', handler: () => new Response(null, { status }) }]);
    await expect(apiRequest('/x', { method: 'POST' })).resolves.toBeUndefined();
  });

  it('returns undefined for an empty 200 body', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/x',
        handler: () => new Response('', { status: 200, headers: { 'content-type': 'application/json' } }),
      },
    ]);
    await expect(apiRequest('/x')).resolves.toBeUndefined();
  });

  it('returns text for non-JSON content types', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/x',
        handler: () => new Response('plain body', { status: 200, headers: { 'content-type': 'text/plain' } }),
      },
    ]);
    await expect(apiRequest('/x')).resolves.toBe('plain body');
  });

  it('returns text when no content-type header is present', async () => {
    const bare = {
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: new Headers(),
      text: () => Promise.resolve('raw'),
    } as unknown as Response;
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(bare)),
    );
    await expect(apiRequest('/x')).resolves.toBe('raw');
  });

  it('returns undefined for invalid JSON on success', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/x',
        handler: () =>
          new Response('{not json', { status: 200, headers: { 'content-type': 'application/json' } }),
      },
    ]);
    await expect(apiRequest('/x')).resolves.toBeUndefined();
  });

  it('maps invalid JSON on an error to a generic ApiError', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/x',
        handler: () =>
          new Response('{oops', {
            status: 500,
            statusText: 'Internal Server Error',
            headers: { 'content-type': 'application/json' },
          }),
      },
    ]);
    const error = (await apiRequest('/x').catch((e: unknown) => e)) as ApiError;
    expect(error.code).toBe('http_500');
    expect(error.message).toBe('Internal Server Error');
    expect(error.correlationId).toBeNull();
    expect(error.supportReference).toBe(error.requestId);
  });

  it('ignores JSON error bodies that do not match the error schema', async () => {
    mockFetch([
      { method: 'GET', path: '/api/v1/x', handler: () => jsonResponse({ error: { code: 1 } }, 400) },
      { method: 'GET', path: '/api/v1/y', handler: () => jsonResponse({ error: null }, 400) },
      { method: 'GET', path: '/api/v1/z', handler: () => jsonResponse({ detail: 'x' }, 400) },
    ]);
    for (const path of ['/x', '/y', '/z']) {
      const error = (await apiRequest(path).catch((e: unknown) => e)) as ApiError;
      expect(error.code).toBe('http_400');
    }
  });

  it('uses a default message when the response has no statusText', async () => {
    mockFetch([{ method: 'GET', path: '/api/v1/x', handler: () => new Response(null, { status: 418 }) }]);
    const error = (await apiRequest('/x').catch((e: unknown) => e)) as ApiError;
    expect(error.message).toBe('Request failed with status 418.');
  });

  it('maps a 401 without a JSON body to authentication_required and still calls the handler', async () => {
    const handler = vi.fn();
    const restore = setUnauthorizedHandler(handler);
    mockFetch([{ method: 'GET', path: '/api/v1/x', handler: () => new Response(null, { status: 401 }) }]);
    const error = (await apiRequest('/x').catch((e: unknown) => e)) as ApiError;
    restore();
    expect(error.code).toBe('authentication_required');
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('treats a non-array details value as no details', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/x',
        handler: () =>
          jsonResponse(
            { error: { code: 'validation_failed', message: 'bad', correlation_id: 'c', details: 'oops' } },
            422,
          ),
      },
    ]);
    const error = (await apiRequest('/x').catch((e: unknown) => e)) as ApiError;
    expect(error.details).toEqual([]);
    expect(error.fieldErrors).toEqual([]);
  });

  it('re-throws AbortError unchanged (not mapped to network_error)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new DOMException('aborted', 'AbortError')));
    const controller = new AbortController();
    const error = await apiRequest('/x', { signal: controller.signal }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(DOMException);
    expect((error as DOMException).name).toBe('AbortError');
    expect(isApiError(error)).toBe(false);
  });

  it('maps other DOMExceptions to a network error with the request id', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new DOMException('nope', 'NetworkError'));
    vi.stubGlobal('fetch', fetchMock);
    const error = (await apiRequest('/x').catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(0);
    expect(error.message).toMatch(/Could not reach the NUVEXA API/);
    const sent = new Headers((fetchMock.mock.calls[0] as [string, RequestInit])[1].headers);
    expect(error.requestId).toBe(sent.get(REQUEST_ID_HEADER));
  });

  it('passes the abort signal through to fetch', async () => {
    const { fetchMock } = mockFetch([{ method: 'GET', path: '/api/v1/x', handler: () => jsonResponse({}) }]);
    const controller = new AbortController();
    await apiRequest('/x', { signal: controller.signal });
    expect(fetchMock.mock.calls[0]![1]?.signal).toBe(controller.signal);
  });

  it('keeps our request id when the server does not echo one', async () => {
    mockFetch([{ method: 'GET', path: '/api/v1/x', handler: () => errorResponse(500, 'internal') }]);
    const error = (await apiRequest('/x').catch((e: unknown) => e)) as ApiError;
    expect(error.requestId).toMatch(/[0-9a-f-]{16,}/);
  });
});

describe('ApiError', () => {
  function make(status: number, code = 'x', correlationId?: string | null) {
    return new ApiError({ status, code, message: 'm', requestId: 'req-1', correlationId });
  }

  it('exposes status predicates', () => {
    expect(make(401).isUnauthorized).toBe(true);
    expect(make(403).isForbidden).toBe(true);
    expect(make(404).isNotFound).toBe(true);
    expect(make(409).isConflict).toBe(true);
    expect(make(409).isStaleVersion).toBe(false);
    expect(make(409, 'stale_version').isStaleVersion).toBe(true);
    expect(make(422).isValidation).toBe(true);
    expect(make(0).isNetworkError).toBe(true);
    const ok = make(500);
    expect([ok.isUnauthorized, ok.isForbidden, ok.isNotFound, ok.isConflict, ok.isValidation]).toEqual([
      false,
      false,
      false,
      false,
      false,
    ]);
  });

  it('defaults correlationId/details and falls back to the request id as support reference', () => {
    const error = make(500);
    expect(error.name).toBe('ApiError');
    expect(error).toBeInstanceOf(Error);
    expect(error.correlationId).toBeNull();
    expect(error.details).toEqual([]);
    expect(error.supportReference).toBe('req-1');
    expect(make(500, 'x', 'corr-9').supportReference).toBe('corr-9');
  });

  it('isApiError distinguishes ApiError from other values', () => {
    expect(isApiError(make(500))).toBe(true);
    expect(isApiError(new Error('x'))).toBe(false);
    expect(isApiError({ status: 500 })).toBe(false);
    expect(isApiError(null)).toBe(false);
  });
});

describe('http helpers', () => {
  beforeEach(() => {
    clearCookies();
    document.cookie = 'nuvexa_csrf=tok; path=/';
  });
  afterEach(() => {
    clearCookies();
    vi.unstubAllGlobals();
  });

  it('get/post/put/patch/delete use the right method, body and query', async () => {
    const { calls } = mockFetch(
      (['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] as const).map((method) => ({
        method,
        path: '/api/v1/r',
        handler: () => jsonResponse({ method }),
      })),
    );
    await expect(http.get('/r', { query: { a: 1 } })).resolves.toEqual({ method: 'GET' });
    await expect(http.post('/r', { b: 2 })).resolves.toEqual({ method: 'POST' });
    await expect(http.put('/r', { c: 3 })).resolves.toEqual({ method: 'PUT' });
    await expect(http.patch('/r', { d: 4 })).resolves.toEqual({ method: 'PATCH' });
    await expect(http.delete('/r')).resolves.toEqual({ method: 'DELETE' });
    expect(calls.map((c) => [c.method, c.url, c.body])).toEqual([
      ['GET', '/api/v1/r?a=1', undefined],
      ['POST', '/api/v1/r', { b: 2 }],
      ['PUT', '/api/v1/r', { c: 3 }],
      ['PATCH', '/api/v1/r', { d: 4 }],
      ['DELETE', '/api/v1/r', undefined],
    ]);
    expect(calls.map((c) => c.headers.get(CSRF_HEADER))).toEqual([null, 'tok', 'tok', 'tok', 'tok']);
  });
});

describe('navigation helpers', () => {
  it('navigationUrl builds a same-origin API URL with a sanitised redirect_to', () => {
    expect(navigationUrl('/auth/login', '/approvals?view=attention')).toBe(
      `/api/v1/auth/login?redirect_to=${encodeURIComponent('/approvals?view=attention')}`,
    );
    expect(navigationUrl('/auth/login', 'https://evil.example')).toBe('/api/v1/auth/login?redirect_to=%2F');
    expect(navigationUrl('/integrations/microsoft/connect', '//evil')).toBe(
      '/api/v1/integrations/microsoft/connect?redirect_to=%2F',
    );
  });

  it('safeRedirectPath rejects API paths, login paths and empty values; honours a custom fallback', () => {
    expect(safeRedirectPath('/api/v1/auth/logout')).toBe('/');
    expect(safeRedirectPath('/login?redirect_to=/x')).toBe('/');
    expect(safeRedirectPath('')).toBe('/');
    expect(safeRedirectPath(undefined, '/inbox')).toBe('/inbox');
    expect(safeRedirectPath('javascript:alert(1)', '/inbox')).toBe('/inbox');
    expect(safeRedirectPath('/\\evil', '/home')).toBe('/home');
    expect(safeRedirectPath('/workflows/run-1')).toBe('/workflows/run-1');
    expect(safeRedirectPath('/apiary')).toBe('/apiary');
  });

  // Regression (Prompt 5): an allow-list, not a deny-list — the URL parser strips tab/CR/LF anywhere.
  it.each([
    '/\t/evil.example',
    '/\n/evil.example',
    '/\r/evil.example',
    '/ /evil.example',
    '/inbox\n',
    '/inbox?q=a\tb',
    '/inbox\\..\\..\\evil',
    '/\u0000/evil.example',
    '/\u007f',
    '/ /evil.example',
    '/inbox?q=café',
    '/／／evil.example',
  ])('safeRedirectPath rejects %j (whitespace, controls, backslash, non-ASCII)', (value) => {
    expect(safeRedirectPath(value, '/home')).toBe('/home');
  });

  it('safeRedirectPath keeps in-app paths built by the app (URLSearchParams "+", hash, sub-delimiters)', () => {
    for (const path of [
      '/inbox?q=foo+bar&category=bug',
      '/audit?event_type=action.approved&offset=50',
      '/actions/act-1#history',
      "/emails/e-1?from=a@b.example&tags=(x),y;z:w!~*'$",
    ]) {
      expect(safeRedirectPath(path)).toBe(path);
      expect(new URL(path, window.location.origin).origin).toBe(window.location.origin);
    }
  });

  it('API_BASE and header names are the documented constants', () => {
    expect(API_BASE).toBe('/api/v1');
    expect(CSRF_COOKIE).toBe('nuvexa_csrf');
    expect(CSRF_HEADER).toBe('X-CSRF-Token');
    expect(REQUEST_ID_HEADER).toBe('X-Request-ID');
  });
});

describe('unauthorized handler registry', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('setUnauthorizedHandler returns a restore function that reinstates the previous handler', async () => {
    const first = vi.fn();
    const second = vi.fn();
    const restoreFirst = setUnauthorizedHandler(first);
    const restoreSecond = setUnauthorizedHandler(second);
    mockFetch([
      { method: 'GET', path: '/api/v1/x', handler: () => errorResponse(401, 'authentication_required') },
    ]);
    await apiRequest('/x').catch(() => undefined);
    restoreSecond();
    await apiRequest('/x').catch(() => undefined);
    restoreFirst();
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).toHaveBeenCalledTimes(1);
  });
});

describe('default unauthorized handler (fresh module)', () => {
  const original = window.location;

  function stubLocation(pathname: string, search = '') {
    const assign = vi.fn();
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...original, pathname, search, assign },
    });
    return assign;
  }

  afterEach(() => {
    Object.defineProperty(window, 'location', { configurable: true, value: original });
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  async function freshClient() {
    vi.resetModules();
    return import('./client');
  }

  it('does nothing when already on /login', async () => {
    const client = await freshClient();
    const assign = stubLocation('/login', '?redirect_to=%2F');
    mockFetch([
      { method: 'GET', path: '/api/v1/x', handler: () => errorResponse(401, 'authentication_required') },
    ]);
    await client.apiRequest('/x').catch(() => undefined);
    expect(assign).not.toHaveBeenCalled();
  });

  it('redirects only once even if several requests fail with 401', async () => {
    const client = await freshClient();
    const assign = stubLocation('/inbox', '?q=outage');
    mockFetch([
      { method: 'GET', path: '/api/v1/x', handler: () => errorResponse(401, 'authentication_required') },
    ]);
    await Promise.all([
      client.apiRequest('/x').catch(() => undefined),
      client.apiRequest('/x').catch(() => undefined),
    ]);
    await client.apiRequest('/x').catch(() => undefined);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledWith(`/login?redirect_to=${encodeURIComponent('/inbox?q=outage')}`);
  });
});
