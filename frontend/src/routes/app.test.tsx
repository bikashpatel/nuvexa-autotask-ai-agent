import { MantineProvider } from '@mantine/core';
import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createTestQueryClient, jsonResponse, mockFetch, TEST_SESSION, type Route } from '../test/utils';
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
  mockFetch([
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
  return router;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('application routes', () => {
  it('redirects unauthenticated users to /login with redirect_to, showing demo personas', async () => {
    const router = renderApp('/approvals?view=attention', [
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
