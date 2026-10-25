/**
 * Adversarial QA — UI state that can mislead a reviewer.
 *
 * (a) Queued execute/reconcile jobs: the action card watches a queued job for 120 s (WATCH_MS in
 *     ActionCard.tsx). Once the watch expires polling stops, so the card must stop promising that the
 *     status "updates automatically" and must not keep the decision buttons disabled forever.
 * (b) List filters live in the URL. When browser Back/Forward changes the query of the page that is
 *     already mounted, the search boxes must show the query the list is actually filtered by.
 */
import { MantineProvider } from '@mantine/core';
import { QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActionDetail } from '../api/types';
import { ActionCard } from '../features/actions/ActionCard';
import { AuditLogPage } from '../features/audit/AuditLogPage';
import { SessionContext } from '../features/auth/session';
import { InboxPage } from '../features/inbox/InboxPage';
import { theme } from '../theme/theme';
import {
  ALL_ACTION_PERMISSIONS,
  actionSummary,
  adoAction,
  approvalFixture,
  auditEvent,
  emailListItem,
  page,
} from './fixtures';
import {
  TEST_SESSION,
  callsTo,
  createTestQueryClient,
  jsonResponse,
  mockFetch,
  queryOf,
  renderWithProviders,
} from './utils';

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// --- (a) Queued-job watch expiry -------------------------------------------------------------------

const DETAIL = '/api/v1/actions/act-1';

/** An approved action whose execute job is accepted (202) but never picked up by a worker. */
async function queueAnExecutionThatIsNeverPickedUp() {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const approved: ActionDetail = adoAction({
    status: 'approved',
    current_approval: approvalFixture(),
    approvals: [approvalFixture()],
    permissions: ALL_ACTION_PERMISSIONS,
  });
  const { calls } = mockFetch([
    { method: 'GET', path: DETAIL, handler: () => jsonResponse(approved) },
    { method: 'GET', path: `${DETAIL}/history`, handler: () => jsonResponse([]) },
    {
      method: 'GET',
      path: '/api/v1/integrations/azure-devops/projects',
      handler: () => jsonResponse({ items: [] }),
    },
    {
      method: 'POST',
      path: `${DETAIL}/execute`,
      handler: () => jsonResponse({ action: actionSummary({ ...approved }), job_id: 'job-1' }, 202),
    },
  ]);
  renderWithProviders(<ActionCard actionId="act-1" />, { withNotifications: true });
  await user.click(await screen.findByTestId('execute-button'));
  await user.click(await screen.findByTestId('confirm-execute'));
  expect(await screen.findByText(/Queued — waiting for a worker/)).toBeInTheDocument();
  // While the watch is active the card legitimately blocks re-submission.
  expect(screen.getByTestId('execute-button')).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled();

  // Let the 120 s watch run out, then confirm polling really stopped.
  await act(() => vi.advanceTimersByTimeAsync(125_000));
  const afterDeadline = callsTo(calls, 'GET', DETAIL).length;
  await act(() => vi.advanceTimersByTimeAsync(30_000));
  expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(afterDeadline);
}

describe('(a) queued-job watch expiry', () => {
  // DEFECT: after the 120 s watch expires polling stops, but the card still says "Status updates automatically".
  it('stops claiming the status "updates automatically" once polling has stopped', async () => {
    await queueAnExecutionThatIsNeverPickedUp();
    expect(screen.queryByText(/updates automatically/)).toBeNull();
  });

  // DEFECT: after the watch expires `queued` stays true (ActionCard ignores watch.until), so the buttons stay disabled until a reload.
  it('does not leave the decision buttons disabled after the watch has expired', async () => {
    await queueAnExecutionThatIsNeverPickedUp();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Cancel proposal' })).toBeEnabled();
  });
});

// --- (b) Search boxes vs. browser history ---------------------------------------------------------

/** Like renderWithProviders, but with a real history stack so Back/Forward can be exercised. */
function renderWithHistory(ui: ReactNode, entries: string[], index: number) {
  const router = createMemoryRouter([{ path: '*', element: ui }], {
    initialEntries: entries,
    initialIndex: index,
  });
  render(
    <MantineProvider theme={theme} env="test">
      <QueryClientProvider client={createTestQueryClient()}>
        <SessionContext.Provider value={TEST_SESSION}>
          <RouterProvider router={router} />
        </SessionContext.Provider>
      </QueryClientProvider>
    </MantineProvider>,
  );
  return router;
}

describe('(b) list search boxes follow Back/Forward navigation', () => {
  // DEFECT: InboxPage seeds its search state from ?q= only on mount (useState), so Back shows the stale query.
  it('Inbox: Back to ?q=alpha shows "alpha" in the search box (the list is already filtered by alpha)', async () => {
    const { calls } = mockFetch([
      { method: 'GET', path: '/api/v1/emails', handler: () => jsonResponse(page([emailListItem()])) },
    ]);
    const router = renderWithHistory(<InboxPage />, ['/inbox?q=alpha', '/inbox?q=beta'], 1);
    const search = await screen.findByRole('textbox', { name: 'Search' });
    expect(search).toHaveValue('beta');

    await act(() => router.navigate(-1));
    expect(router.state.location.search).toBe('?q=alpha');
    // The URL is the source of truth: the list request already uses the new query…
    await waitFor(() =>
      expect(callsTo(calls, 'GET', '/api/v1/emails').map((c) => queryOf(c).get('q'))).toContain('alpha'),
    );
    // …so the box must show it too.
    await waitFor(() => expect(search).toHaveValue('alpha'), { timeout: 1000 });
  });

  // DEFECT: same stale-input pattern when Back/Forward (or a push to the mounted page, e.g. the sidebar "Inbox" link) drops ?q=.
  it('Inbox: Back to a URL without ?q= clears the search box', async () => {
    mockFetch([
      { method: 'GET', path: '/api/v1/emails', handler: () => jsonResponse(page([emailListItem()])) },
    ]);
    const router = renderWithHistory(<InboxPage />, ['/inbox', '/inbox?q=alpha'], 1);
    const search = await screen.findByRole('textbox', { name: 'Search' });
    expect(search).toHaveValue('alpha');
    await act(() => router.navigate(-1));
    expect(router.state.location.search).toBe('');
    await waitFor(() => expect(search).toHaveValue(''), { timeout: 1000 });
  });

  // DEFECT: AuditLogPage seeds its event-type box from ?event_type= only on mount, so Back shows the stale filter.
  it('Audit log: Back to ?event_type=action.approved shows that value in the event-type box', async () => {
    const { calls } = mockFetch([
      { method: 'GET', path: '/api/v1/audit-events', handler: () => jsonResponse(page([auditEvent()])) },
    ]);
    const router = renderWithHistory(
      <AuditLogPage />,
      ['/audit?event_type=action.approved', '/audit?event_type=action.rejected'],
      1,
    );
    const box = await screen.findByRole('textbox', { name: 'Event type' });
    expect(box).toHaveValue('action.rejected');

    await act(() => router.navigate(-1));
    await waitFor(() =>
      expect(
        callsTo(calls, 'GET', '/api/v1/audit-events').map((c) => queryOf(c).get('event_type')),
      ).toContain('action.approved'),
    );
    await waitFor(() => expect(box).toHaveValue('action.approved'), { timeout: 1000 });
  });
});
