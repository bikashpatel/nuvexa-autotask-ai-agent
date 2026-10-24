import { MantineProvider } from '@mantine/core';
import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RouterProvider, createMemoryRouter, useLocation } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { expectNoSeriousA11yViolations } from '../../test/axe';
import { PERSONAS } from '../../test/fixtures';
import {
  callsTo,
  createTestQueryClient,
  deferred,
  errorResponse,
  jsonResponse,
  mockFetch,
  type Route,
} from '../../test/utils';
import { theme } from '../../theme/theme';
import { FullPageLoader, RequireAuth } from './RequireAuth';
import { useSession } from './session';

function Protected() {
  const session = useSession();
  return <p>Protected content for {session.user.display_name}</p>;
}

function LoginProbe() {
  const location = useLocation();
  return <p data-testid="login-location">{`${location.pathname}${location.search}`}</p>;
}

function renderGuard(initialPath: string, routes: Route[]) {
  const { calls } = mockFetch(routes);
  const queryClient = createTestQueryClient();
  const router = createMemoryRouter(
    [
      { path: '/login', element: <LoginProbe /> },
      {
        element: <RequireAuth />,
        children: [
          { path: '/approvals', element: <Protected /> },
          { path: '/emails/:id', element: <Protected /> },
        ],
      },
    ],
    { initialEntries: [initialPath] },
  );
  render(
    <MantineProvider theme={theme} env="test">
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  return { router, calls, queryClient };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('<FullPageLoader>', () => {
  it('announces a polite status with the default label', () => {
    render(
      <MantineProvider theme={theme} env="test">
        <FullPageLoader />
      </MantineProvider>,
    );
    const status = screen.getByRole('status');
    expect(status).toHaveAttribute('aria-live', 'polite');
    expect(status).toHaveTextContent('Loading NUVEXA…');
  });
});

describe('<RequireAuth>', () => {
  it('shows a session loader while /auth/me is pending', async () => {
    const pending = deferred<Response>();
    renderGuard('/approvals', [{ method: 'GET', path: '/api/v1/auth/me', handler: () => pending.promise }]);
    expect(screen.getByRole('status')).toHaveTextContent('Checking your session…');
    expect(screen.queryByText(/Protected content/)).toBeNull();
    pending.resolve(jsonResponse(PERSONAS.approver));
    expect(await screen.findByText('Protected content for Avery Approver')).toBeInTheDocument();
  });

  it('redirects a 401 to /login, preserving the return path (path + search) in redirect_to', async () => {
    const { router } = renderGuard('/approvals?view=attention&type=propose_email_reply', [
      {
        method: 'GET',
        path: '/api/v1/auth/me',
        handler: () => errorResponse(401, 'authentication_required', { correlationId: null }),
      },
    ]);
    const target = '/approvals?view=attention&type=propose_email_reply';
    expect(await screen.findByTestId('login-location')).toHaveTextContent(
      `/login?redirect_to=${encodeURIComponent(target)}`,
    );
    expect(router.state.location.pathname).toBe('/login');
    expect(new URLSearchParams(router.state.location.search).get('redirect_to')).toBe(target);
    // replace: the protected URL is not left behind in history.
    expect(router.state.historyAction).toBe('REPLACE');
    expect(screen.queryByText(/Protected content/)).toBeNull();
  });

  it('preserves deep-link params of nested routes', async () => {
    const { router } = renderGuard('/emails/email-42', [
      {
        method: 'GET',
        path: '/api/v1/auth/me',
        handler: () => errorResponse(401, 'authentication_required'),
      },
    ]);
    await screen.findByTestId('login-location');
    expect(new URLSearchParams(router.state.location.search).get('redirect_to')).toBe('/emails/email-42');
  });

  it('shows a non-401 failure with its support reference and recovers on Retry', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls, router } = renderGuard('/approvals', [
      {
        method: 'GET',
        path: '/api/v1/auth/me',
        handler: () =>
          fail
            ? errorResponse(500, 'internal_error', {
                message: 'The service is temporarily unavailable.',
                correlationId: 'corr-me-500',
              })
            : jsonResponse(PERSONAS.member),
      },
    ]);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load your session');
    expect(alert).toHaveTextContent('The service is temporarily unavailable.');
    expect(alert).toHaveTextContent('corr-me-500');
    expect(alert).toHaveTextContent('internal_error, HTTP 500');
    // Not a 401: the user stays on the requested page.
    expect(router.state.location.pathname).toBe('/approvals');
    await expectNoSeriousA11yViolations();

    fail = false;
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Protected content for Morgan Member')).toBeInTheDocument();
    expect(callsTo(calls, 'GET', '/api/v1/auth/me')).toHaveLength(2);
  });

  it('provides the session to the protected tree on success', async () => {
    renderGuard('/approvals', [
      { method: 'GET', path: '/api/v1/auth/me', handler: () => jsonResponse(PERSONAS.admin) },
    ]);
    expect(await screen.findByText('Protected content for Ada Admin')).toBeInTheDocument();
  });
});
