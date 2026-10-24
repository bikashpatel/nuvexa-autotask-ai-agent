import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setUnauthorizedHandler } from '../../api/client';
import { queryKeys } from '../../api/queryKeys';
import type { AuthConfigOut } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { authConfig, PERSONAS } from '../../test/fixtures';
import {
  callsTo,
  deferred,
  errorResponse,
  jsonResponse,
  mockFetch,
  renderWithProviders,
  type Route,
} from '../../test/utils';
import { LoginPage } from './LoginPage';

function LocationProbe() {
  const location = useLocation();
  return <p data-testid="location">{`${location.pathname}${location.search}`}</p>;
}

const unauthenticated: Route = {
  method: 'GET',
  path: '/api/v1/auth/me',
  handler: () => errorResponse(401, 'authentication_required', { correlationId: null }),
};

function configRoute(config: Partial<AuthConfigOut> = {}): Route {
  return { method: 'GET', path: '/api/v1/auth/config', handler: () => jsonResponse(authConfig(config)) };
}

function renderLogin(
  search = '',
  routes: Route[] = [unauthenticated, configRoute()],
  withNotifications = false,
) {
  const fetch = mockFetch(routes);
  const result = renderWithProviders(<LoginPage />, {
    session: null,
    path: '/login',
    route: `/login${search}`,
    withNotifications,
    extraRoutes: [
      { path: '/', element: <LocationProbe /> },
      { path: '/approvals', element: <LocationProbe /> },
      { path: '/inbox', element: <LocationProbe /> },
    ],
  });
  return { ...fetch, ...result };
}

function clearCookies() {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

let restoreUnauthorized: () => void = () => undefined;

beforeEach(() => {
  // The page probes /auth/me with skipAuthRedirect; any global redirect would be a bug.
  restoreUnauthorized = setUnauthorizedHandler(() => {
    throw new Error('LoginPage must never trigger the global 401 redirect');
  });
});

afterEach(() => {
  restoreUnauthorized();
  clearCookies();
  vi.unstubAllGlobals();
});

const PERSONA_NAMES = ['Admin', 'Approver', 'Member', 'Viewer'] as const;

describe('<LoginPage> sign-in options', () => {
  it('shows a busy skeleton while the auth config loads', async () => {
    const pending = deferred<Response>();
    renderLogin('', [
      unauthenticated,
      { method: 'GET', path: '/api/v1/auth/config', handler: () => pending.promise },
    ]);
    const busy = screen.getByLabelText('Loading sign-in options');
    expect(busy).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    pending.resolve(jsonResponse(authConfig()));
    expect(await screen.findByRole('button', { name: 'Sign in as demo Admin' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Loading sign-in options')).toBeNull();
  });

  it('shows a config error with its support reference and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = renderLogin('', [
      unauthenticated,
      {
        method: 'GET',
        path: '/api/v1/auth/config',
        handler: () =>
          fail
            ? errorResponse(503, 'service_unavailable', {
                message: 'API is starting.',
                correlationId: 'corr-cfg',
              })
            : jsonResponse(authConfig()),
      },
    ]);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load sign-in options');
    expect(alert).toHaveTextContent('API is starting.');
    expect(alert).toHaveTextContent('corr-cfg');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('button', { name: 'Sign in as demo Viewer' })).toBeInTheDocument();
    expect(callsTo(calls, 'GET', '/api/v1/auth/config')).toHaveLength(2);
  });

  it('Entra enabled: a full-page link to /api/v1/auth/login carrying the sanitised redirect_to', async () => {
    renderLogin(`?redirect_to=${encodeURIComponent('/approvals?view=attention')}`, [
      unauthenticated,
      configRoute({ entra_login_enabled: true }),
    ]);
    const link = await screen.findByRole('link', { name: 'Sign in with Microsoft' });
    expect(link).toHaveAttribute(
      'href',
      `/api/v1/auth/login?redirect_to=${encodeURIComponent('/approvals?view=attention')}`,
    );
    expect(screen.queryByText(/not configured for this environment/)).toBeNull();
  });

  it.each([
    ['an absolute URL', 'https://evil.example/phish'],
    ['a protocol-relative URL', '//evil.example'],
    ['a backslash trick', '/\\evil.example'],
    ['the login page itself', '/login?redirect_to=/x'],
    ['an API path', '/api/v1/auth/logout'],
    ['a javascript: URL', 'javascript:alert(1)'],
  ])('never forwards %s as redirect_to (falls back to /)', async (_label, evil) => {
    renderLogin(`?redirect_to=${encodeURIComponent(evil)}`, [
      unauthenticated,
      configRoute({ entra_login_enabled: true }),
    ]);
    const link = await screen.findByRole('link', { name: 'Sign in with Microsoft' });
    expect(link).toHaveAttribute('href', '/api/v1/auth/login?redirect_to=%2F');
  });

  it('Entra disabled: a disabled button described by the reason, including the environment', async () => {
    renderLogin('', [unauthenticated, configRoute({ entra_login_enabled: false, environment: 'staging' })]);
    const button = await screen.findByRole('button', { name: 'Sign in with Microsoft' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-describedby', 'entra-disabled-reason');
    expect(button).toHaveAccessibleDescription(/not configured for this environment \(\s*staging\)/);
    expect(screen.queryByRole('link', { name: 'Sign in with Microsoft' })).toBeNull();
  });

  it('lists the four demo personas only when demo login is enabled', async () => {
    renderLogin();
    for (const name of PERSONA_NAMES) {
      expect(await screen.findByRole('button', { name: `Sign in as demo ${name}` })).toBeEnabled();
    }
    expect(screen.getByText('Demo mode — choose a persona')).toBeInTheDocument();
    expect(screen.getByText('NUVEXA AutoTask · development · demo mode')).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
  });

  it('hides personas and the demo-mode suffix when demo login is disabled', async () => {
    renderLogin('', [
      unauthenticated,
      configRoute({
        demo_login_enabled: false,
        demo_mode: false,
        environment: 'production',
        entra_login_enabled: true,
      }),
    ]);
    expect(await screen.findByRole('link', { name: 'Sign in with Microsoft' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Sign in as demo/ })).toBeNull();
    expect(screen.queryByText('Demo mode — choose a persona')).toBeNull();
    expect(screen.getByText('NUVEXA AutoTask · production')).toBeInTheDocument();
  });

  it('explains a failed Microsoft sign-in (?error=…)', async () => {
    renderLogin('?error=access_denied');
    expect(await screen.findByText('Sign-in did not complete')).toBeInTheDocument();
    expect(screen.getByText(/Microsoft sign-in was cancelled or failed/)).toBeInTheDocument();
  });

  it('does not show the sign-in error alert without ?error', async () => {
    renderLogin();
    await screen.findByRole('button', { name: 'Sign in as demo Admin' });
    expect(screen.queryByText('Sign-in did not complete')).toBeNull();
  });
});

describe('<LoginPage> demo sign-in', () => {
  it('POSTs the persona with CSRF, disables every persona while pending, sends one request, then navigates', async () => {
    const user = userEvent.setup();
    document.cookie = 'nuvexa_csrf=csrf-login-token; path=/';
    const login = deferred<Response>();
    const { calls, router, queryClient } = renderLogin(
      `?redirect_to=${encodeURIComponent('/approvals?view=approved')}`,
      [
        unauthenticated,
        configRoute(),
        { method: 'POST', path: '/api/v1/auth/demo-login', handler: () => login.promise },
      ],
    );
    const member = await screen.findByRole('button', { name: 'Sign in as demo Member' });
    await user.dblClick(member);

    await waitFor(() => {
      for (const name of PERSONA_NAMES) {
        expect(screen.getByRole('button', { name: `Sign in as demo ${name}` })).toBeDisabled();
      }
    });
    await user.click(screen.getByRole('button', { name: 'Sign in as demo Admin' }));
    const posts = callsTo(calls, 'POST', '/api/v1/auth/demo-login');
    expect(posts).toHaveLength(1);
    expect(posts[0]!.body).toEqual({ persona: 'member' });
    expect(posts[0]!.headers.get('X-CSRF-Token')).toBe('csrf-login-token');
    // Still on the login page until the server confirms.
    expect(router.state.location.pathname).toBe('/login');

    login.resolve(jsonResponse(PERSONAS.member));
    expect(await screen.findByTestId('location')).toHaveTextContent('/approvals?view=approved');
    expect(router.state.historyAction).toBe('REPLACE');
    expect(queryClient.getQueryData(queryKeys.me)).toEqual(PERSONAS.member);
  });

  it('falls back to / after sign-in when redirect_to is unsafe', async () => {
    const user = userEvent.setup();
    const { router } = renderLogin(`?redirect_to=${encodeURIComponent('https://evil.example')}`, [
      unauthenticated,
      configRoute(),
      { method: 'POST', path: '/api/v1/auth/demo-login', handler: () => jsonResponse(PERSONAS.viewer) },
    ]);
    await user.click(await screen.findByRole('button', { name: 'Sign in as demo Viewer' }));
    expect(await screen.findByTestId('location')).toHaveTextContent(/^\/$/);
    expect(router.state.location.pathname).toBe('/');
  });

  it('activates a persona from the keyboard (Tab + Enter)', async () => {
    const user = userEvent.setup();
    const { calls } = renderLogin('', [
      unauthenticated,
      configRoute(),
      { method: 'POST', path: '/api/v1/auth/demo-login', handler: () => jsonResponse(PERSONAS.admin) },
    ]);
    const admin = await screen.findByRole('button', { name: 'Sign in as demo Admin' });
    // The disabled Microsoft button is skipped: the first tab stop is the first persona.
    await user.tab();
    expect(admin).toHaveFocus();
    await user.keyboard('{Enter}');
    await screen.findByTestId('location');
    expect(callsTo(calls, 'POST', '/api/v1/auth/demo-login')[0]!.body).toEqual({ persona: 'admin' });
  });

  it('shows an error toast with the support reference and stays on the page when sign-in fails', async () => {
    const user = userEvent.setup();
    const { router } = renderLogin(
      '',
      [
        unauthenticated,
        configRoute(),
        {
          method: 'POST',
          path: '/api/v1/auth/demo-login',
          handler: () =>
            errorResponse(403, 'demo_login_disabled', {
              message: 'Demo login is disabled.',
              correlationId: 'corr-demo-1',
            }),
        },
      ],
      true,
    );
    await user.click(await screen.findByRole('button', { name: 'Sign in as demo Approver' }));
    expect(await screen.findByText('Demo sign-in failed')).toBeInTheDocument();
    expect(screen.getByText('Demo login is disabled.')).toBeInTheDocument();
    expect(screen.getByText('demo_login_disabled · ref corr-demo-1')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/login');
    // Personas are usable again for another attempt.
    expect(screen.getByRole('button', { name: 'Sign in as demo Approver' })).toBeEnabled();
  });
});

describe('<LoginPage> when already signed in', () => {
  it('redirects straight to the (safe) redirect_to', async () => {
    const { router, calls } = renderLogin(`?redirect_to=${encodeURIComponent('/inbox?q=outage')}`, [
      { method: 'GET', path: '/api/v1/auth/me', handler: () => jsonResponse(PERSONAS.member) },
      configRoute(),
    ]);
    expect(await screen.findByTestId('location')).toHaveTextContent('/inbox?q=outage');
    expect(router.state.historyAction).toBe('REPLACE');
    expect(callsTo(calls, 'POST', '/api/v1/auth/demo-login')).toHaveLength(0);
  });
});
