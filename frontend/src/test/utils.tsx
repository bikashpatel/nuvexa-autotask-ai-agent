import { MantineProvider } from '@mantine/core';
import { Notifications } from '@mantine/notifications';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, type MatcherFunction, type RenderResult } from '@testing-library/react';
import type { ReactNode } from 'react';
import { RouterProvider, createMemoryRouter, type RouteObject } from 'react-router';
import { vi } from 'vitest';

import type { MeOut } from '../api/types';
import { SessionContext } from '../features/auth/session';
import { theme } from '../theme/theme';

/**
 * Text matcher on an element's *full* text content, including child elements such as the <bdi>
 * isolates around untrusted values. Only the innermost matching element matches, so getBy* stays
 * unambiguous (the default matcher only sees an element's own text nodes).
 */
export function textContent(expected: string | RegExp): MatcherFunction {
  const matches = (text: string) => (typeof expected === 'string' ? text === expected : expected.test(text));
  return (_content, element) =>
    element !== null &&
    matches(element.textContent ?? '') &&
    !Array.from(element.children).some((child) => matches(child.textContent ?? ''));
}

export const TEST_SESSION: MeOut = {
  user: {
    id: 'u-1',
    email: 'approver@example.test',
    display_name: 'Avery Approver',
    role: 'approver',
    tenant_id: 't-1',
    tenant_name: 'Contoso',
  },
  permissions: [
    'email:read',
    'email:sync',
    'workflow:run',
    'action:edit',
    'action:approve',
    'action:execute',
    'action:approve_any',
    'audit:read_all',
  ],
};

export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity, staleTime: Infinity, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}

export interface RenderOptions {
  session?: MeOut | null;
  queryClient?: QueryClient;
  /** Route pattern the UI is mounted on (e.g. `/actions/:actionId`). */
  path?: string;
  /** Initial location. */
  route?: string;
  /** Mount Mantine <Notifications/> so toasts are rendered and can be asserted. */
  withNotifications?: boolean;
  /** Extra routes next to the UI (navigation targets, e.g. `{ path: '/login', element: … }`). */
  extraRoutes?: RouteObject[];
}

export type RenderWithProvidersResult = RenderResult & {
  queryClient: QueryClient;
  router: ReturnType<typeof createMemoryRouter>;
};

export function renderWithProviders(
  ui: ReactNode,
  {
    session = TEST_SESSION,
    queryClient = createTestQueryClient(),
    path = '*',
    route = '/',
    withNotifications = false,
    extraRoutes = [],
  }: RenderOptions = {},
): RenderWithProvidersResult {
  const router = createMemoryRouter([...extraRoutes, { path, element: ui }], { initialEntries: [route] });
  const result = render(
    <MantineProvider theme={theme} env="test">
      {withNotifications ? <Notifications /> : null}
      <QueryClientProvider client={queryClient}>
        <SessionContext.Provider value={session}>
          <RouterProvider router={router} />
        </SessionContext.Provider>
      </QueryClientProvider>
    </MantineProvider>,
  );
  return { ...result, queryClient, router };
}

/** Wrapper for `renderHook` (TanStack Query + Mantine + a session; no router). */
export function createWrapper({
  queryClient = createTestQueryClient(),
  session = TEST_SESSION,
}: { queryClient?: QueryClient; session?: MeOut | null } = {}) {
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MantineProvider theme={theme} env="test">
        <QueryClientProvider client={queryClient}>
          <SessionContext.Provider value={session}>{children}</SessionContext.Provider>
        </QueryClientProvider>
      </MantineProvider>
    );
  }
  return { Wrapper, queryClient };
}

export function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

/** An API error in the documented schema `{ error: { code, message, correlation_id, details } }`. */
export function errorResponse(
  status: number,
  code: string,
  {
    message = `Request failed: ${code}`,
    correlationId = `corr-${code}`,
    details = [],
    headers = {},
  }: {
    message?: string;
    correlationId?: string | null;
    details?: Record<string, unknown>[];
    headers?: Record<string, string>;
  } = {},
): Response {
  return jsonResponse({ error: { code, message, correlation_id: correlationId, details } }, status, headers);
}

export interface RecordedCall {
  method: string;
  url: string;
  headers: Headers;
  body: unknown;
}

type Handler = (call: RecordedCall) => Response | Promise<Response>;

export interface Route {
  method: string;
  path: string | RegExp;
  handler: Handler;
}

/** Route-based fetch mock. Unmatched requests fail loudly with 599 so tests notice. */
export function mockFetch(routes: Route[]) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const method = (init?.method ?? 'GET').toUpperCase();
    const headers = new Headers(init?.headers);
    const rawBody = typeof init?.body === 'string' ? init.body : undefined;
    const call: RecordedCall = {
      method,
      url,
      headers,
      body: rawBody ? (JSON.parse(rawBody) as unknown) : undefined,
    };
    calls.push(call);
    const pathname = url.split('?')[0] ?? url;
    const route = routes.find(
      (r) =>
        r.method === method && (typeof r.path === 'string' ? r.path === pathname : r.path.test(pathname)),
    );
    if (!route) {
      return jsonResponse(
        { error: { code: 'unmocked', message: `${method} ${url}`, correlation_id: null, details: [] } },
        599,
      );
    }
    return route.handler(call);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, calls };
}

/** Calls matching a method and exact pathname (query string ignored). */
export function callsTo(calls: RecordedCall[], method: string, pathname: string): RecordedCall[] {
  return calls.filter((c) => c.method === method && (c.url.split('?')[0] ?? c.url) === pathname);
}

/** Parsed query parameters of a recorded call (repeated keys preserved via getAll). */
export function queryOf(call: RecordedCall): URLSearchParams {
  const index = call.url.indexOf('?');
  return new URLSearchParams(index === -1 ? '' : call.url.slice(index + 1));
}

/** A promise you resolve later — used to hold a request "in flight". */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
