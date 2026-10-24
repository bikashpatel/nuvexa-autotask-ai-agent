import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse, mockFetch } from '../test/utils';
import {
  ApiError,
  CSRF_HEADER,
  REQUEST_ID_HEADER,
  apiRequest,
  buildQuery,
  parseFieldErrors,
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
