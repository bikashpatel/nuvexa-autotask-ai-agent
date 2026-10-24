import { QueryClient } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setUnauthorizedHandler } from '../../api/client';
import { authConfig, PERSONAS } from '../../test/fixtures';
import { callsTo, createWrapper, errorResponse, jsonResponse, mockFetch } from '../../test/utils';
import { useAuthConfig, useHasPermission, useMeQuery, useSession } from './session';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useSession', () => {
  it('throws outside <RequireAuth> (no session in context)', () => {
    const { Wrapper } = createWrapper({ session: null });
    // React logs the thrown error; keep the test output clean.
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    expect(() => renderHook(() => useSession(), { wrapper: Wrapper })).toThrow(
      'useSession must be used inside <RequireAuth>',
    );
  });

  it('returns the session provided by the context', () => {
    const { Wrapper } = createWrapper({ session: PERSONAS.admin });
    const { result } = renderHook(() => useSession(), { wrapper: Wrapper });
    expect(result.current).toBe(PERSONAS.admin);
    expect(result.current.user.display_name).toBe('Ada Admin');
  });
});

describe('useHasPermission', () => {
  it('is true only for permissions the session holds', () => {
    const { Wrapper } = createWrapper({ session: PERSONAS.admin });
    const { result } = renderHook(
      () => ({
        settings: useHasPermission('settings:write'),
        audit: useHasPermission('audit:read_all'),
      }),
      { wrapper: Wrapper },
    );
    expect(result.current).toEqual({ settings: true, audit: true });
  });

  it('is false for a permission the role lacks (viewer cannot sync or approve)', () => {
    const { Wrapper } = createWrapper({ session: PERSONAS.viewer });
    const { result } = renderHook(
      () => [
        useHasPermission('email:read'),
        useHasPermission('email:sync'),
        useHasPermission('action:approve'),
      ],
      { wrapper: Wrapper },
    );
    expect(result.current).toEqual([true, false, false]);
  });

  it('is false (never throws) without a session', () => {
    const { Wrapper } = createWrapper({ session: null });
    const { result } = renderHook(() => useHasPermission('email:read'), { wrapper: Wrapper });
    expect(result.current).toBe(false);
  });
});

describe('useMeQuery', () => {
  it('GETs /auth/me and exposes the session', async () => {
    const { calls } = mockFetch([
      { method: 'GET', path: '/api/v1/auth/me', handler: () => jsonResponse(PERSONAS.member) },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useMeQuery(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(PERSONAS.member);
    expect(callsTo(calls, 'GET', '/api/v1/auth/me')).toHaveLength(1);
  });

  it('on 401 does not trigger the global login redirect and does not retry', async () => {
    const handler = vi.fn();
    const restore = setUnauthorizedHandler(handler);
    const { calls } = mockFetch([
      {
        method: 'GET',
        path: '/api/v1/auth/me',
        handler: () => errorResponse(401, 'authentication_required', { correlationId: null }),
      },
    ]);
    // A client whose default would retry — the hook must override it with retry: false.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 3, retryDelay: 0 } } });
    const { Wrapper } = createWrapper({ queryClient });
    const { result } = renderHook(() => useMeQuery(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    restore();
    expect(handler).not.toHaveBeenCalled();
    expect(result.current.error).toMatchObject({ status: 401, code: 'authentication_required' });
    expect(callsTo(calls, 'GET', '/api/v1/auth/me')).toHaveLength(1);
  });
});

describe('useAuthConfig', () => {
  it('GETs /auth/config', async () => {
    const config = authConfig({ environment: 'staging', entra_login_enabled: true });
    const { calls } = mockFetch([
      { method: 'GET', path: '/api/v1/auth/config', handler: () => jsonResponse(config) },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useAuthConfig(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.data).toEqual(config));
    expect(callsTo(calls, 'GET', '/api/v1/auth/config')).toHaveLength(1);
  });

  it('does not redirect to /login when the config endpoint answers 401', async () => {
    const handler = vi.fn();
    const restore = setUnauthorizedHandler(handler);
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/auth/config',
        handler: () => errorResponse(401, 'authentication_required'),
      },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useAuthConfig(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    restore();
    expect(handler).not.toHaveBeenCalled();
  });
});
