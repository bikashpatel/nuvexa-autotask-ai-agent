import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActionOut, Page } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { PERSONAS, XSS_IMG, actionSummary, analysis, page } from '../../test/fixtures';
import {
  callsTo,
  deferred,
  errorResponse,
  jsonResponse,
  mockFetch,
  queryOf,
  renderWithProviders,
  type RecordedCall,
  type RenderOptions,
} from '../../test/utils';
import { ApprovalsPage } from './ApprovalsPage';

const LIST = '/api/v1/actions';

type Reply = Response | Promise<Response>;

function setup(
  list: (call: RecordedCall) => Page<ActionOut> | Reply = () => page([actionSummary()]),
  {
    analysisReply = () => jsonResponse(analysis()),
    ...options
  }: { analysisReply?: (call: RecordedCall) => Reply } & RenderOptions = {},
) {
  const mock = mockFetch([
    {
      method: 'GET',
      path: LIST,
      handler: (call) => {
        const result = list(call);
        return result instanceof Response || result instanceof Promise ? result : jsonResponse(result);
      },
    },
    { method: 'GET', path: /^\/api\/v1\/workflows\/[^/]+\/analysis$/, handler: analysisReply },
  ]);
  return { ...mock, ...renderWithProviders(<ApprovalsPage />, options) };
}

const listCalls = (calls: RecordedCall[]) => callsTo(calls, 'GET', LIST);

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('<ApprovalsPage> states', () => {
  it('shows a busy skeleton while loading', () => {
    const hold = deferred<Response>();
    setup(() => hold.promise);
    expect(screen.getByLabelText('Loading approvals')).toHaveAttribute('aria-busy', 'true');
  });

  it('shows the error with its correlation id and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = setup(() =>
      fail
        ? errorResponse(502, 'upstream_error', { correlationId: 'corr-approvals' })
        : page([actionSummary()]),
    );
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('corr-approvals')).toBeInTheDocument();
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('table', { name: 'Pending approval' })).toBeInTheDocument();
    expect(listCalls(calls)).toHaveLength(2);
  });

  it.each([
    ['/', 'Nothing is waiting for approval', true],
    ['/?view=approved', 'No approved actions waiting for execution', false],
    ['/?view=attention', 'No failed or uncertain actions', false],
  ])('empty copy for %s', async (route, title, offersInbox) => {
    setup(() => page([]), { route });
    expect(await screen.findByText(title)).toBeInTheDocument();
    const inbox = screen.queryByRole('link', { name: 'Go to inbox' });
    if (offersInbox) expect(inbox).toHaveAttribute('href', '/inbox');
    else expect(inbox).toBeNull();
  });

  it('describes tenant-wide scope only for action:approve_any', async () => {
    const { unmount } = setup(() => page([]), { session: PERSONAS.approver });
    expect(await screen.findByText(/Every proposal across the tenant/)).toBeInTheDocument();
    unmount();
    setup(() => page([]), { session: PERSONAS.member });
    expect(await screen.findByText(/Your proposals that need a decision/)).toBeInTheDocument();
  });
});

describe('<ApprovalsPage> rows', () => {
  it('renders type, title link, subject, confidence, status, created and a named Review button', async () => {
    const user = userEvent.setup();
    const { container, router } = setup(
      () =>
        page([
          actionSummary(),
          actionSummary({
            id: 'act-2',
            workflow_run_id: 'run-2',
            action_type: 'propose_email_reply',
            title: 'Reply to Dana',
            email_subject: null,
          }),
        ]),
      {
        analysisReply: (call) =>
          call.url.includes('run-2') ? errorResponse(404, 'not_found') : jsonResponse(analysis()),
        extraRoutes: [{ path: '/actions/:actionId', element: <p>Review screen</p> }],
      },
    );
    const table = await screen.findByRole('table', { name: 'Pending approval' });
    const [first, second] = within(table)
      .getAllByRole('row')
      .slice(1)
      .map((r) => within(r));
    expect(first!.getByText('Azure DevOps work item')).toBeInTheDocument();
    expect(
      first!.getByRole('link', { name: 'Create bug: Checkout fails with 500 for EU customers' }),
    ).toHaveAttribute('href', '/actions/act-1');
    expect(first!.getByText('URGENT: checkout broken')).toBeInTheDocument();
    expect(await first!.findByText('0.62 · medium')).toBeInTheDocument();
    expect(first!.getByLabelText('Status: Pending approval')).toBeInTheDocument();
    expect(first!.getByRole('time')).toHaveAttribute('datetime', '2026-10-24T10:00:00Z');

    expect(second!.getByText('Reply draft')).toBeInTheDocument();
    // No email subject and an unavailable analysis both render an em dash.
    await waitFor(() => expect(second!.getAllByText('—')).toHaveLength(2));

    const review = screen.getByRole('link', { name: 'Review “Reply to Dana”' });
    expect(review).toHaveAttribute('href', '/actions/act-2');
    await expectNoSeriousA11yViolations(container);
    await user.click(review);
    expect(await screen.findByText('Review screen')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/actions/act-2');
  });

  it('caches the analysis per run (one request for two actions of the same run)', async () => {
    const { calls } = setup(() => page([actionSummary(), actionSummary({ id: 'act-2', title: 'Second' })]));
    await screen.findAllByText('0.62 · medium');
    expect(callsTo(calls, 'GET', '/api/v1/workflows/run-1/analysis')).toHaveLength(1);
  });

  it('renders hostile titles as text', async () => {
    const { container } = setup(() => page([actionSummary({ title: XSS_IMG, email_subject: XSS_IMG })]));
    expect(await screen.findByRole('link', { name: XSS_IMG })).toBeInTheDocument();
    expect(container.querySelector('table img')).toBeNull();
  });
});

describe('<ApprovalsPage> views, filters, pagination', () => {
  it('requests pending_approval by default and ignores invalid URL params', async () => {
    const { calls } = setup(() => page([actionSummary()]), { route: '/?view=bogus&type=nope&offset=-3' });
    await screen.findByRole('table', { name: 'Pending approval' });
    const params = queryOf(listCalls(calls)[0]!);
    expect(params.getAll('status')).toEqual(['pending_approval']);
    expect(params.get('action_type')).toBeNull();
    expect(params.get('offset')).toBe('0');
    expect(params.get('limit')).toBe('25');
  });

  it('switches views via the segmented control and sends repeated status params', async () => {
    const user = userEvent.setup();
    const { calls, router } = setup(() => page([actionSummary()]), { route: '/?offset=25' });
    await screen.findByRole('table', { name: 'Pending approval' });
    expect(screen.getByRole('radiogroup', { name: 'Approval queue view' })).toBeInTheDocument();

    await user.click(screen.getByRole('radio', { name: 'Needs attention' }));
    await waitFor(() => expect(router.state.location.search).toBe('?view=attention'));
    await waitFor(() =>
      expect(queryOf(listCalls(calls).at(-1)!).getAll('status')).toEqual(['failed', 'uncertain']),
    );
    expect(listCalls(calls).at(-1)!.url).toContain('status=failed&status=uncertain');
    expect(await screen.findByRole('table', { name: 'Needs attention' })).toBeInTheDocument();

    await user.click(screen.getByRole('radio', { name: 'Approved — ready to execute' }));
    await waitFor(() => expect(queryOf(listCalls(calls).at(-1)!).getAll('status')).toEqual(['approved']));

    await user.click(screen.getByRole('radio', { name: 'Pending approval' }));
    await waitFor(() => expect(router.state.location.search).toBe(''));
  });

  it('filters by action type and clears the filter', async () => {
    const user = userEvent.setup();
    const { calls, router } = setup(() => page([actionSummary()]));
    await screen.findByRole('table', { name: 'Pending approval' });
    await user.click(screen.getByRole('combobox', { name: 'Filter by action type' }));
    await user.click(screen.getByRole('option', { name: 'Reply draft' }));
    await waitFor(() => expect(router.state.location.search).toBe('?type=propose_email_reply'));
    await waitFor(() =>
      expect(queryOf(listCalls(calls).at(-1)!).get('action_type')).toBe('propose_email_reply'),
    );
  });

  it('accepts a valid type from the URL', async () => {
    const { calls } = setup(() => page([actionSummary()]), { route: '/?type=mark_no_action&view=approved' });
    await screen.findByRole('table', { name: 'Approved — ready to execute' });
    const params = queryOf(listCalls(calls)[0]!);
    expect(params.get('action_type')).toBe('mark_no_action');
    expect(params.getAll('status')).toEqual(['approved']);
    expect(screen.getByRole('combobox', { name: 'Filter by action type' })).toHaveValue('No action');
  });

  it('paginates with offset', async () => {
    const user = userEvent.setup();
    const { calls, router } = setup((call) =>
      page([actionSummary()], { total: 60, offset: Number(queryOf(call).get('offset')) }),
    );
    await screen.findByText('1–25 of 60');
    await user.click(screen.getByRole('button', { name: 'Page 3' }));
    await waitFor(() => expect(router.state.location.search).toBe('?offset=50'));
    expect(await screen.findByText('51–60 of 60')).toBeInTheDocument();
    expect(queryOf(listCalls(calls).at(-1)!).get('offset')).toBe('50');
    await user.click(screen.getByRole('button', { name: 'Page 1' }));
    await waitFor(() => expect(router.state.location.search).toBe(''));
  });

  it('refreshes the queue every 15 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = setup(() => page([actionSummary()]));
    await screen.findByRole('table', { name: 'Pending approval' });
    expect(listCalls(calls)).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(14_000));
    expect(listCalls(calls)).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(1_500));
    await waitFor(() => expect(listCalls(calls)).toHaveLength(2));
  });
});
