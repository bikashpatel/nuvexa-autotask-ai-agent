import { MantineProvider } from '@mantine/core';
import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import {
  actionSummary,
  adoAction,
  analysis,
  auditEvent,
  dashboardSummary,
  emailDetail,
  emailListItem,
  integrationsOut,
  page,
  PERSONAS,
  settingsOut,
  taskFixture,
  workflowDetail,
  workflowRun,
} from '../test/fixtures';
import {
  callsTo,
  createTestQueryClient,
  errorResponse,
  jsonResponse,
  mockFetch,
  TEST_SESSION,
  type Route,
} from '../test/utils';
import { theme } from '../theme/theme';
import { routes } from './router';

const authConfig = {
  app_name: 'NUVEXA AutoTask',
  environment: 'development',
  demo_mode: true,
  demo_login_enabled: true,
  entra_login_enabled: false,
};

function renderApp(initialPath: string, extraRoutes: Route[]) {
  const { calls } = mockFetch([
    { method: 'GET', path: '/api/v1/auth/config', handler: () => jsonResponse(authConfig) },
    ...extraRoutes,
  ]);
  const router = createMemoryRouter(routes, { initialEntries: [initialPath] });
  render(
    <MantineProvider theme={theme} env="test">
      <QueryClientProvider client={createTestQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  return { router, calls };
}

// Load (transform) every lazy page chunk once up front, so per-test timing measures rendering only
// and not first-time module compilation on a busy machine.
beforeAll(async () => {
  const pages = routes[1]?.children?.[0]?.children ?? [];
  await Promise.all(
    pages.map((route) => (typeof route.lazy === 'function' ? route.lazy() : Promise.resolve())),
  );
}, 60_000);

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('application routes', () => {
  it('redirects unauthenticated users to /login with redirect_to, showing demo personas', async () => {
    const { router } = renderApp('/approvals?view=attention', [
      {
        method: 'GET',
        path: '/api/v1/auth/me',
        handler: () =>
          jsonResponse(
            {
              error: {
                code: 'authentication_required',
                message: 'Authentication is required.',
                correlation_id: null,
                details: [],
              },
            },
            401,
          ),
      },
    ]);
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/login');
    expect(router.state.location.search).toBe(
      `?redirect_to=${encodeURIComponent('/approvals?view=attention')}`,
    );
    // Entra disabled → explained, not clickable.
    expect(await screen.findByRole('button', { name: /Sign in with Microsoft/ })).toBeDisabled();
    expect(screen.getByText(/not configured for this environment/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign in as demo Approver' })).toBeInTheDocument();
  });

  it('renders the authenticated shell with user, role, tenant and demo badge', async () => {
    renderApp('/', [
      { method: 'GET', path: '/api/v1/auth/me', handler: () => jsonResponse(TEST_SESSION) },
      {
        method: 'GET',
        path: '/api/v1/actions',
        handler: () => jsonResponse({ items: [], total: 3, limit: 1, offset: 0 }),
      },
      {
        method: 'GET',
        path: '/api/v1/dashboard/summary',
        handler: () =>
          jsonResponse({
            emails_total: 0,
            emails_analyzed: 0,
            actions_proposed: 0,
            pending_approvals: 0,
            completed_actions: 0,
            failed_actions: 0,
            uncertain_actions: 0,
            rejected_actions: 0,
            workflows_by_status: {},
            categories: {},
            avg_approval_wait_seconds: null,
            llm: { total_tokens: 0, estimated_cost_usd: null, avg_latency_ms: null },
            recent_activity: [],
          }),
      },
    ]);
    expect(await screen.findByText('No emails yet — sync your mailbox')).toBeInTheDocument();
    expect(screen.getByLabelText('Demo mode active')).toBeInTheDocument();
    expect(screen.getByText('Avery Approver')).toBeInTheDocument();
    expect(screen.getByText('Approver · Contoso')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Overview/ })).toHaveAttribute('aria-current', 'page');
    expect(await screen.findByLabelText('3 pending approvals')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Switch to (dark|light) theme/ })).toBeInTheDocument();
  });
});

const unauthenticated: Route = {
  method: 'GET',
  path: '/api/v1/auth/me',
  handler: () => errorResponse(401, 'authentication_required', { correlationId: null }),
};

const signedIn: Route = {
  method: 'GET',
  path: '/api/v1/auth/me',
  handler: () => jsonResponse(PERSONAS.approver),
};

/** Minimal, realistic API for every page (shapes from app/schemas/api.py). */
const pageApi: Route[] = [
  { method: 'GET', path: '/api/v1/dashboard/summary', handler: () => jsonResponse(dashboardSummary()) },
  { method: 'GET', path: '/api/v1/emails', handler: () => jsonResponse(page([emailListItem()])) },
  { method: 'GET', path: '/api/v1/emails/email-1', handler: () => jsonResponse(emailDetail()) },
  { method: 'GET', path: '/api/v1/workflows', handler: () => jsonResponse(page([workflowRun()])) },
  { method: 'GET', path: '/api/v1/workflows/run-1', handler: () => jsonResponse(workflowDetail()) },
  { method: 'GET', path: '/api/v1/workflows/run-1/analysis', handler: () => jsonResponse(analysis()) },
  { method: 'GET', path: '/api/v1/actions', handler: () => jsonResponse(page([actionSummary()])) },
  { method: 'GET', path: '/api/v1/actions/act-1', handler: () => jsonResponse(adoAction()) },
  { method: 'GET', path: '/api/v1/actions/act-1/history', handler: () => jsonResponse([auditEvent()]) },
  { method: 'GET', path: '/api/v1/integrations', handler: () => jsonResponse(integrationsOut()) },
  {
    method: 'GET',
    path: '/api/v1/integrations/azure-devops/projects',
    handler: () => jsonResponse({ items: [] }),
  },
  {
    method: 'GET',
    path: '/api/v1/health/ready',
    handler: () => jsonResponse({ status: 'ok', version: '0.1.0', checks: { database: 'ok' } }),
  },
  { method: 'GET', path: '/api/v1/settings', handler: () => jsonResponse(settingsOut()) },
  { method: 'GET', path: '/api/v1/audit-events', handler: () => jsonResponse(page([auditEvent()])) },
  { method: 'GET', path: '/api/v1/tasks', handler: () => jsonResponse(page([taskFixture()])) },
];

describe('protected deep links', () => {
  it.each([
    ['/', 'Welcome back, Avery', 'Overview'],
    ['/inbox', 'Inbox', 'Inbox'],
    ['/emails/email-1', 'URGENT: checkout broken', 'Inbox'],
    ['/approvals', 'Approvals', 'Approvals'],
    ['/actions/act-1', 'URGENT: checkout broken', 'Approvals'],
    ['/workflows', 'Workflows', 'Workflows'],
    ['/workflows/run-1', 'URGENT: checkout broken', 'Workflows'],
    ['/integrations', 'Integrations', 'Integrations'],
    ['/settings', 'Settings', 'Settings'],
    ['/audit', 'Audit log', 'Audit log'],
    ['/tasks', 'Tasks & drafts', 'Tasks & drafts'],
  ])('%s renders its page inside the shell', async (path, heading, navLabel) => {
    renderApp(path, [signedIn, ...pageApi]);
    expect(
      await screen.findByRole('heading', { level: 1, name: heading }, { timeout: 3000 }),
    ).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Main navigation' });
    const current = within(nav)
      .getAllByRole('link')
      .filter((link) => link.getAttribute('aria-current') === 'page');
    expect(current).toHaveLength(1);
    expect(current[0]).toHaveAccessibleName(new RegExp(`^${navLabel}`));
  });

  it('renders the 404 page inside the shell for an unknown path', async () => {
    const { router } = renderApp('/does/not/exist', [signedIn, ...pageApi]);
    expect(await screen.findByText('Page not found')).toBeInTheDocument();
    expect(await screen.findByRole('navigation', { name: 'Main navigation' })).toBeInTheDocument();
    await userEvent.setup().click(await screen.findByRole('link', { name: 'Back to overview' }));
    // The overview chunk is lazy: the location commits once it has loaded, then the page renders.
    await waitFor(() => expect(router.state.location.pathname).toBe('/'));
    expect(await screen.findByRole('heading', { level: 1, name: 'Welcome back, Avery' })).toBeInTheDocument();
  });

  it('sends an unauthenticated deep link to /login and returns there after demo sign-in', async () => {
    const user = userEvent.setup();
    let session: typeof PERSONAS.approver | null = null;
    const { router, calls } = renderApp('/workflows/run-1?tab=events', [
      {
        method: 'GET',
        path: '/api/v1/auth/me',
        handler: () =>
          session
            ? jsonResponse(session)
            : errorResponse(401, 'authentication_required', { correlationId: null }),
      },
      {
        method: 'POST',
        path: '/api/v1/auth/demo-login',
        handler: () => {
          session = PERSONAS.approver;
          return jsonResponse(session);
        },
      },
      ...pageApi,
    ]);
    await screen.findByRole('heading', { name: 'Sign in' });
    expect(router.state.location.pathname).toBe('/login');
    expect(new URLSearchParams(router.state.location.search).get('redirect_to')).toBe(
      '/workflows/run-1?tab=events',
    );

    await user.click(await screen.findByRole('button', { name: 'Sign in as demo Approver' }));
    expect(
      await screen.findByRole('heading', { level: 1, name: 'URGENT: checkout broken' }, { timeout: 3000 }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/workflows/run-1');
    expect(router.state.location.search).toBe('?tab=events');
    expect(callsTo(calls, 'POST', '/api/v1/auth/demo-login')).toHaveLength(1);
  });

  it('a 401 from a page API call (session expired) redirects once to /login, preserving the return path', async () => {
    const assign = vi.fn();
    const original = window.location;
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...original, pathname: '/inbox', search: '?q=outage', assign },
    });
    try {
      renderApp('/inbox?q=outage', [
        signedIn,
        {
          method: 'GET',
          path: '/api/v1/emails',
          handler: () => errorResponse(401, 'authentication_required'),
        },
        {
          method: 'GET',
          path: '/api/v1/actions',
          handler: () => errorResponse(401, 'authentication_required'),
        },
      ]);
      await waitFor(() => expect(assign).toHaveBeenCalled());
      // Both the inbox list and the pending badge got a 401, but only one navigation happens.
      await screen.findByRole('alert');
      expect(assign).toHaveBeenCalledTimes(1);
      expect(assign).toHaveBeenCalledWith(`/login?redirect_to=${encodeURIComponent('/inbox?q=outage')}`);
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original });
    }
  });

  it('keeps the user signed out of protected pages: no page API is called before the session check', async () => {
    const { calls } = renderApp('/approvals', [unauthenticated, ...pageApi]);
    await screen.findByRole('heading', { name: 'Sign in' });
    expect(callsTo(calls, 'GET', '/api/v1/actions')).toHaveLength(0);
    expect(callsTo(calls, 'GET', '/api/v1/dashboard/summary')).toHaveLength(0);
  });
});
