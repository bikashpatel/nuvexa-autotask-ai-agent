import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, type RenderResult } from '@testing-library/react';
import type { ReactNode } from 'react';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { vi } from 'vitest';

import type { MeOut } from '../api/types';
import { SessionContext } from '../features/auth/session';
import { theme } from '../theme/theme';

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

export function renderWithProviders(
  ui: ReactNode,
  {
    session = TEST_SESSION,
    queryClient = createTestQueryClient(),
    path = '*',
    route = '/',
  }: { session?: MeOut | null; queryClient?: QueryClient; path?: string; route?: string } = {},
): RenderResult & { queryClient: QueryClient } {
  const router = createMemoryRouter([{ path, element: ui }], { initialEntries: [route] });
  const result = render(
    <MantineProvider theme={theme} env="test">
      <QueryClientProvider client={queryClient}>
        <SessionContext.Provider value={session}>
          <RouterProvider router={router} />
        </SessionContext.Provider>
      </QueryClientProvider>
    </MantineProvider>,
  );
  return { ...result, queryClient };
}

export function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
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

/** A promise you resolve later — used to hold a request "in flight". */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
