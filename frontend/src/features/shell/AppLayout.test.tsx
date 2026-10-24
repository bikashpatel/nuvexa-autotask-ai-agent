import { MantineProvider } from '@mantine/core';
import { QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { lazy } from 'react';
import { RouterProvider, createMemoryRouter, useLocation } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { queryKeys } from '../../api/queryKeys';
import type { AuthConfigOut, MeOut } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { authConfig, page, PERSONAS, sessionFor } from '../../test/fixtures';
import {
  callsTo,
  createTestQueryClient,
  deferred,
  errorResponse,
  jsonResponse,
  mockFetch,
  queryOf,
  type Route,
} from '../../test/utils';
import { theme } from '../../theme/theme';
import { SessionContext } from '../auth/session';
import { AppLayout } from './AppLayout';
import { NAV_ITEMS } from './navigation';

function LocationProbe() {
  const location = useLocation();
  return <p data-testid="location">{location.pathname}</p>;
}

const lazyGate = deferred<{ default: () => React.JSX.Element }>();
const LazyPage = lazy(() => lazyGate.promise);

interface Options {
  session?: MeOut;
  config?: Partial<AuthConfigOut>;
  pendingTotal?: () => number;
  extra?: Route[];
}

function renderShell(
  initialPath = '/',
  { session = PERSONAS.approver, config, pendingTotal, extra = [] }: Options = {},
) {
  const fetch = mockFetch([
    { method: 'GET', path: '/api/v1/auth/config', handler: () => jsonResponse(authConfig(config)) },
    {
      method: 'GET',
      path: '/api/v1/actions',
      handler: () => jsonResponse(page([], { total: pendingTotal ? pendingTotal() : 0, limit: 1 })),
    },
    ...extra,
  ]);
  const queryClient = createTestQueryClient();
  const child = <LocationProbe />;
  const router = createMemoryRouter(
    [
      { path: '/login', element: <p data-testid="login-page">Login</p> },
      {
        element: (
          <SessionContext.Provider value={session}>
            <AppLayout />
          </SessionContext.Provider>
        ),
        children: [
          { path: '/', element: child },
          { path: '/inbox', element: child },
          { path: '/emails/:id', element: child },
          { path: '/approvals', element: child },
          { path: '/actions/:id', element: child },
          { path: '/workflows', element: child },
          { path: '/tasks', element: child },
          { path: '/integrations', element: child },
          { path: '/settings', element: child },
          { path: '/audit', element: child },
          { path: '/lazy', element: <LazyPage /> },
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
  return { ...fetch, router, queryClient };
}

function clearCookies() {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  clearCookies();
});

describe('<AppLayout> header', () => {
  it('links the brand home and shows demo-mode and environment badges from the auth config', async () => {
    renderShell('/inbox');
    const home = screen.getByRole('link', { name: 'NUVEXA AutoTask home' });
    expect(home).toHaveAttribute('href', '/');
    expect(await screen.findByLabelText('Demo mode active')).toHaveTextContent('Demo mode');
    expect(screen.getByText('development')).toBeInTheDocument();
  });

  it('hides the demo badge outside demo mode and the environment badge in production', async () => {
    const { queryClient } = renderShell('/', { config: { demo_mode: false, environment: 'production' } });
    await waitFor(() => expect(queryClient.getQueryData(queryKeys.authConfig)).toBeDefined());
    expect(screen.queryByLabelText('Demo mode active')).toBeNull();
    expect(screen.queryByText('production')).toBeNull();
  });

  it('shows the environment badge for non-production environments without demo mode', async () => {
    renderShell('/', { config: { demo_mode: false, environment: 'staging' } });
    expect(await screen.findByText('staging')).toBeInTheDocument();
    expect(screen.queryByLabelText('Demo mode active')).toBeNull();
  });

  it('shows the user, role and tenant and initials in the avatar', async () => {
    renderShell('/');
    const account = screen.getByRole('button', { name: 'Account menu for Avery Approver' });
    expect(within(account).getByText('AA')).toBeInTheDocument();
    expect(within(account).getByText('Avery Approver')).toBeInTheDocument();
    expect(within(account).getByText('Approver · Contoso')).toBeInTheDocument();
    await screen.findByLabelText('Demo mode active');
  });

  it.each([
    ['Cher', 'C'],
    ['  ada   lovelace  king ', 'AL'],
    ['', '?'],
  ])('derives avatar initials from %j → %s', async (displayName, expected) => {
    renderShell('/', { session: sessionFor('member', { display_name: displayName }) });
    const account = screen.getByRole('button', { name: /^Account menu for/ });
    expect(within(account).getByText(expected)).toBeInTheDocument();
    await screen.findByLabelText('Demo mode active');
  });

  it('has no serious accessibility violations', async () => {
    renderShell('/approvals', { pendingTotal: () => 3 });
    await screen.findByLabelText('3 pending approvals');
    await expectNoSeriousA11yViolations();
  });
});

describe('<AppLayout> account menu', () => {
  it('opens on click and shows email, role and tenant', async () => {
    const user = userEvent.setup();
    renderShell('/', { session: PERSONAS.admin });
    const trigger = screen.getByRole('button', { name: 'Account menu for Ada Admin' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    await user.click(trigger);
    const menu = await screen.findByRole('menu');
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(within(menu).getByText('Signed in as')).toBeInTheDocument();
    expect(within(menu).getByText('admin@contoso.example')).toBeInTheDocument();
    expect(within(menu).getByText('Admin')).toBeInTheDocument();
    expect(within(menu).getByText('Contoso')).toBeInTheDocument();
    expect(within(menu).getByRole('menuitem', { name: 'Sign out' })).toBeEnabled();
  });

  it('opens from the keyboard and closes on Escape', async () => {
    const user = userEvent.setup();
    renderShell('/');
    const trigger = screen.getByRole('button', { name: 'Account menu for Avery Approver' });
    trigger.focus();
    await user.keyboard('{Enter}');
    expect(await screen.findByRole('menu')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('signs out via POST /auth/logout (with CSRF), shows progress, clears the cache and goes to /login', async () => {
    const user = userEvent.setup();
    document.cookie = 'nuvexa_csrf=csrf-shell; path=/';
    const logout = deferred<Response>();
    const { calls, router, queryClient } = renderShell('/inbox', {
      extra: [{ method: 'POST', path: '/api/v1/auth/logout', handler: () => logout.promise }],
    });
    queryClient.setQueryData(queryKeys.emails, { cached: true });

    await user.click(screen.getByRole('button', { name: 'Account menu for Avery Approver' }));
    await user.click(await screen.findByRole('menuitem', { name: 'Sign out' }));
    const posts = callsTo(calls, 'POST', '/api/v1/auth/logout');
    expect(posts).toHaveLength(1);
    expect(posts[0]!.headers.get('X-CSRF-Token')).toBe('csrf-shell');

    // While the request is in flight the item reads "Signing out…" and is disabled.
    await user.click(screen.getByRole('button', { name: 'Account menu for Avery Approver' }));
    const item = await screen.findByRole('menuitem', { name: 'Signing out…' });
    expect(item).toBeDisabled();
    expect(router.state.location.pathname).toBe('/inbox');

    logout.resolve(new Response(null, { status: 204 }));
    expect(await screen.findByTestId('login-page')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/login');
    expect(router.state.historyAction).toBe('REPLACE');
    expect(queryClient.getQueryData(queryKeys.emails)).toBeUndefined();
  });

  it('still clears the session and goes to /login when logout fails', async () => {
    const user = userEvent.setup();
    const { router, queryClient } = renderShell('/', {
      extra: [
        {
          method: 'POST',
          path: '/api/v1/auth/logout',
          handler: () => errorResponse(500, 'internal_error'),
        },
      ],
    });
    queryClient.setQueryData(queryKeys.me, PERSONAS.approver);
    await user.click(screen.getByRole('button', { name: 'Account menu for Avery Approver' }));
    await user.click(await screen.findByRole('menuitem', { name: 'Sign out' }));
    expect(await screen.findByTestId('login-page')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/login');
    expect(queryClient.getQueryData(queryKeys.me)).toBeUndefined();
  });
});

describe('<AppLayout> navigation', () => {
  it('renders every navigation item as a link inside the labelled main navigation', async () => {
    renderShell('/');
    const nav = screen.getByRole('navigation', { name: 'Main navigation' });
    for (const item of NAV_ITEMS) {
      const link = within(nav).getByRole('link', { name: new RegExp(`^${item.label}`) });
      expect(link).toHaveAttribute('href', item.to);
    }
    await screen.findByLabelText('Demo mode active');
  });

  it.each([
    ['/', 'Overview'],
    ['/inbox', 'Inbox'],
    ['/emails/email-1', 'Inbox'],
    ['/approvals', 'Approvals'],
    ['/actions/act-1', 'Approvals'],
    ['/workflows', 'Workflows'],
    ['/audit', 'Audit log'],
  ])('on %s only "%s" is the current page', async (path, label) => {
    renderShell(path);
    const nav = screen.getByRole('navigation', { name: 'Main navigation' });
    const current = within(nav)
      .getAllByRole('link')
      .filter((link) => link.getAttribute('aria-current') === 'page');
    expect(current).toHaveLength(1);
    expect(current[0]).toHaveAccessibleName(new RegExp(`^${label}`));
    await screen.findByLabelText('Demo mode active');
  });

  it('navigates when a nav link is clicked and moves aria-current', async () => {
    const user = userEvent.setup();
    const { router } = renderShell('/');
    await user.click(screen.getByRole('link', { name: 'Workflows' }));
    expect(router.state.location.pathname).toBe('/workflows');
    expect(screen.getByTestId('location')).toHaveTextContent('/workflows');
    expect(screen.getByRole('link', { name: 'Workflows' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Overview' })).not.toHaveAttribute('aria-current');
  });

  it('the burger toggles its label and a nav click closes the mobile navigation', async () => {
    const user = userEvent.setup();
    renderShell('/');
    const burger = screen.getByRole('button', { name: 'Open navigation' });
    await user.click(burger);
    expect(screen.getByRole('button', { name: 'Close navigation' })).toBeInTheDocument();
    await user.click(screen.getByRole('link', { name: 'Inbox' }));
    expect(screen.getByRole('button', { name: 'Open navigation' })).toBeInTheDocument();
  });

  it('shows the route skeleton while a lazy page suspends', async () => {
    renderShell('/lazy');
    expect(screen.getByLabelText('Loading')).toHaveAttribute('aria-busy', 'true');
    await act(async () => {
      lazyGate.resolve({ default: () => <p>Lazy page loaded</p> });
      await lazyGate.promise;
    });
    expect(await screen.findByText('Lazy page loaded')).toBeInTheDocument();
    expect(screen.queryByLabelText('Loading')).toBeNull();
  });
});

describe('<AppLayout> pending approvals badge', () => {
  it('asks for one pending_approval item and hides the badge when the total is 0', async () => {
    const { calls } = renderShell('/');
    await waitFor(() => expect(callsTo(calls, 'GET', '/api/v1/actions')).toHaveLength(1));
    const params = queryOf(callsTo(calls, 'GET', '/api/v1/actions')[0]!);
    expect(params.getAll('status')).toEqual(['pending_approval']);
    expect(params.get('limit')).toBe('1');
    expect(params.get('offset')).toBe('0');
    await screen.findByLabelText('Demo mode active');
    expect(screen.queryByLabelText(/pending approvals/)).toBeNull();
  });

  it('shows the count on the Approvals item', async () => {
    renderShell('/', { pendingTotal: () => 7 });
    const badge = await screen.findByLabelText('7 pending approvals');
    expect(badge).toHaveTextContent('7');
    expect(screen.getByRole('link', { name: /^Approvals/ })).toContainElement(badge);
  });

  it('caps the displayed count at 99+', async () => {
    renderShell('/', { pendingTotal: () => 150 });
    expect(await screen.findByLabelText('150 pending approvals')).toHaveTextContent('99+');
  });

  it('refreshes every 30 seconds', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let total = 2;
    const { calls } = renderShell('/', { pendingTotal: () => total });
    expect(await screen.findByLabelText('2 pending approvals')).toBeInTheDocument();
    const before = callsTo(calls, 'GET', '/api/v1/actions').length;

    total = 12;
    await act(() => vi.advanceTimersByTimeAsync(29_000));
    expect(callsTo(calls, 'GET', '/api/v1/actions')).toHaveLength(before);
    await act(() => vi.advanceTimersByTimeAsync(1_500));
    expect(callsTo(calls, 'GET', '/api/v1/actions').length).toBeGreaterThan(before);
    expect(await screen.findByLabelText('12 pending approvals')).toHaveTextContent('12');
  });
});
