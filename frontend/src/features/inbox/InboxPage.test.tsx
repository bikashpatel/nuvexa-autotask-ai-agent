import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { EmailListItem, Page } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { PERSONAS, XSS_IMG, XSS_SCRIPT, emailListItem, page, workflowRun } from '../../test/fixtures';
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
  type Route,
} from '../../test/utils';
import { InboxPage } from './InboxPage';

const LIST = '/api/v1/emails';

const notAnalyzed = emailListItem({
  id: 'email-2',
  subject: 'Invoice question',
  sender_name: null,
  sender_address: 'billing@northwind.example',
  analysis_status: 'not_analyzed',
  category: null,
  priority: null,
  latest_workflow_run_id: null,
  is_read: true,
});

function setup(
  list: (call: RecordedCall) => Page<EmailListItem> | Response | Promise<Response>,
  options: RenderOptions & { extra?: Route[] } = {},
) {
  const { extra = [], ...renderOptions } = options;
  const mock = mockFetch([
    {
      method: 'GET',
      path: LIST,
      handler: (call) => {
        const result = list(call);
        return result instanceof Response || result instanceof Promise ? result : jsonResponse(result);
      },
    },
    ...extra,
  ]);
  const rendered = renderWithProviders(<InboxPage />, renderOptions);
  return { ...mock, ...rendered };
}

const listCalls = (calls: Parameters<typeof callsTo>[0]) => callsTo(calls, 'GET', LIST);

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('<InboxPage> states', () => {
  it('shows a busy table skeleton while loading', () => {
    const pending = deferred<Response>();
    setup(() => pending.promise);
    expect(screen.getByLabelText('Loading emails')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('heading', { name: 'Inbox' })).toBeInTheDocument();
  });

  it('shows the error with its support reference and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = setup(() =>
      fail ? errorResponse(500, 'internal_error', { correlationId: 'corr-inbox' }) : page([emailListItem()]),
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Something went wrong');
    expect(within(alert).getByText('corr-inbox')).toBeInTheDocument();
    expect(alert).toHaveTextContent('internal_error, HTTP 500');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('link', { name: 'URGENT: checkout broken' })).toBeInTheDocument();
    expect(listCalls(calls)).toHaveLength(2);
  });

  it('offers sync + integrations when the inbox is empty (member)', async () => {
    const user = userEvent.setup();
    setup(() => page([]), {
      session: PERSONAS.member,
      extraRoutes: [{ path: '/integrations', element: <p>Integrations page</p> }],
    });
    expect(await screen.findByText('No emails yet — sync your mailbox')).toBeInTheDocument();
    expect(screen.getByText(/Pull the latest messages from your connected mailbox/)).toBeInTheDocument();
    // Header + empty state both offer the sync action.
    expect(screen.getAllByRole('button', { name: 'Sync mailbox' })).toHaveLength(2);
    const integrations = screen.getByRole('link', { name: 'Integrations' });
    expect(integrations).toHaveAttribute('href', '/integrations');
    await user.click(integrations);
    expect(await screen.findByText('Integrations page')).toBeInTheDocument();
  });

  it('tells a viewer to ask someone else, and shows no mutating controls', async () => {
    setup(() => page([]), { session: PERSONAS.viewer });
    expect(
      await screen.findByText('Ask a member or admin to connect and sync a mailbox.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sync mailbox' })).toBeNull();
  });

  it('distinguishes "no matches" when filters are active', async () => {
    setup(() => page([]), { route: '/?category=bug' });
    expect(await screen.findByText('No emails match these filters')).toBeInTheDocument();
    expect(screen.queryByText('No emails yet — sync your mailbox')).toBeNull();
  });
});

describe('<InboxPage> rows', () => {
  it('renders realistic rows with badges, indicators and links', async () => {
    const { container } = setup(
      () =>
        page([emailListItem({ has_attachments: true, content_changed_since_analysis: true }), notAnalyzed]),
      { session: PERSONAS.member },
    );
    const table = await screen.findByRole('table', { name: 'Emails' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);

    const first = within(rows[0]!);
    expect(first.getByText('Dana Customer')).toBeInTheDocument();
    expect(first.getByText('dana@fabrikam.example')).toBeInTheDocument();
    expect(first.getByRole('link', { name: 'URGENT: checkout broken' })).toHaveAttribute(
      'href',
      '/emails/email-1',
    );
    expect(first.getByText('Checkout returns 500 for EU customers…')).toBeInTheDocument();
    expect(first.getByText('Incident')).toBeInTheDocument();
    expect(first.getByLabelText('Priority: High')).toBeInTheDocument();
    expect(first.getByLabelText('Status: Awaiting review')).toBeInTheDocument();
    expect(first.getByLabelText('Has attachments')).toBeInTheDocument();
    expect(first.getByText('Changed')).toBeInTheDocument();
    expect(first.getByText('— email content changed since analysis')).toBeInTheDocument();
    expect(
      first.getByRole('link', { name: 'Open latest workflow for “URGENT: checkout broken”' }),
    ).toHaveAttribute('href', '/workflows/run-1');
    expect(first.getByRole('button', { name: 'Re-analyze “URGENT: checkout broken”' })).toBeEnabled();

    const second = within(rows[1]!);
    expect(second.getByText('billing@northwind.example')).toBeInTheDocument();
    expect(second.getByText('Unclassified')).toBeInTheDocument();
    expect(second.getByLabelText('Status: Not analyzed')).toBeInTheDocument();
    expect(second.queryByRole('link', { name: /Open latest workflow/ })).toBeNull();
    expect(second.getByRole('button', { name: 'Analyze “Invoice question”' })).toBeInTheDocument();

    expect(screen.getByText('1–2 of 2')).toBeInTheDocument();
    await expectNoSeriousA11yViolations(container);
  });

  it('falls back for missing subject/sender', async () => {
    setup(() =>
      page([
        emailListItem({ subject: '', sender_name: null, sender_address: null, latest_workflow_run_id: null }),
      ]),
    );
    expect(await screen.findByRole('link', { name: '(no subject)' })).toBeInTheDocument();
    expect(screen.getByText('Unknown sender')).toBeInTheDocument();
  });

  it('regression: an empty display name falls back to the sender address, never a blank sender', async () => {
    setup(() => page([emailListItem({ sender_name: '', sender_address: 'dana@fabrikam.example' })]));
    const row = (await screen.findByRole('link', { name: 'URGENT: checkout broken' })).closest('tr')!;
    expect(within(row).getByText('dana@fabrikam.example')).toBeInTheDocument();
    expect(within(row).queryByText('Unknown sender')).toBeNull();
  });

  it('renders hostile subject, sender and preview as inert text', async () => {
    const { container } = setup(() =>
      page([emailListItem({ subject: XSS_IMG, sender_name: XSS_SCRIPT, body_preview: `${XSS_IMG}!` })]),
    );
    expect(await screen.findByRole('link', { name: XSS_IMG })).toBeInTheDocument();
    expect(screen.getByText(XSS_SCRIPT)).toBeInTheDocument();
    expect(screen.getByText(`${XSS_IMG}!`)).toBeInTheDocument();
    expect(container.querySelector('table img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });

  it('hides Analyze for a viewer (no workflow:run)', async () => {
    setup(() => page([emailListItem(), notAnalyzed]), { session: PERSONAS.viewer });
    await screen.findByRole('table', { name: 'Emails' });
    expect(screen.queryByRole('button', { name: /Analyze/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Sync mailbox' })).toBeNull();
  });

  it('navigates to the email detail when the subject is clicked', async () => {
    const user = userEvent.setup();
    const { router } = setup(() => page([emailListItem()]), {
      extraRoutes: [{ path: '/emails/:emailId', element: <p>Email detail</p> }],
    });
    await user.click(await screen.findByRole('link', { name: 'URGENT: checkout broken' }));
    expect(await screen.findByText('Email detail')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/emails/email-1');
  });
});

describe('<InboxPage> filters and pagination', () => {
  it('ignores invalid URL filter values', async () => {
    const { calls } = setup(() => page([emailListItem()]), {
      route: '/?category=nope&priority=urgent&analysis_status=weird&offset=abc',
    });
    await screen.findByRole('table', { name: 'Emails' });
    const params = queryOf(listCalls(calls)[0]!);
    expect(params.get('category')).toBeNull();
    expect(params.get('priority')).toBeNull();
    expect(params.get('analysis_status')).toBeNull();
    expect(params.get('offset')).toBe('0');
    expect(params.get('limit')).toBe('25');
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull();
  });

  it('applies valid URL filters to the request', async () => {
    const { calls } = setup(() => page([emailListItem()]), {
      route: '/?category=bug&priority=critical&analysis_status=done&q=outage&offset=50',
    });
    await screen.findByRole('table', { name: 'Emails' });
    const params = queryOf(listCalls(calls)[0]!);
    expect(Object.fromEntries(params)).toEqual({
      q: 'outage',
      category: 'bug',
      priority: 'critical',
      analysis_status: 'done',
      limit: '25',
      offset: '50',
    });
    expect(screen.getByRole('textbox', { name: 'Search' })).toHaveValue('outage');
  });

  it.each([
    ['Category', 'Bug', 'category', 'bug'],
    ['Priority', 'Critical', 'priority', 'critical'],
    ['Analysis status', 'Awaiting review', 'analysis_status', 'awaiting_review'],
  ])(
    'the %s select updates the URL and the request, resetting the offset',
    async (label, option, key, value) => {
      const user = userEvent.setup();
      const { calls, router } = setup(() => page([emailListItem()], { total: 80, offset: 25 }), {
        route: '/?offset=25',
      });
      await screen.findByRole('table', { name: 'Emails' });
      await user.click(screen.getByRole('combobox', { name: label }));
      await user.click(screen.getByRole('option', { name: option }));
      await waitFor(() => expect(router.state.location.search).toBe(`?${key}=${value}`));
      await waitFor(() => expect(queryOf(listCalls(calls).at(-1)!).get(key)).toBe(value));
      expect(queryOf(listCalls(calls).at(-1)!).get('offset')).toBe('0');
    },
  );

  it('debounces the search box (350 ms) into the q parameter and resets the offset', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { calls, router } = setup(() => page([emailListItem()]), { route: '/?offset=25' });
    await screen.findByRole('table', { name: 'Emails' });
    const search = screen.getByRole('textbox', { name: 'Search' });
    await user.type(search, 'outage');
    const before = listCalls(calls).length;
    expect(router.state.location.search).toBe('?offset=25');
    await act(() => vi.advanceTimersByTimeAsync(400));
    expect(router.state.location.search).toBe('?q=outage');
    await waitFor(() => expect(listCalls(calls).length).toBeGreaterThan(before));
    expect(queryOf(listCalls(calls).at(-1)!).get('q')).toBe('outage');

    await user.clear(search);
    await act(() => vi.advanceTimersByTimeAsync(400));
    expect(router.state.location.search).toBe('');
  });

  it('clears every filter and the search box', async () => {
    const user = userEvent.setup();
    const { router } = setup(() => page([]), { route: '/?category=bug&q=outage' });
    await user.click(await screen.findByRole('button', { name: 'Clear filters' }));
    await waitFor(() => expect(router.state.location.search).toBe(''));
    expect(screen.getByRole('textbox', { name: 'Search' })).toHaveValue('');
    expect(await screen.findByText('No emails yet — sync your mailbox')).toBeInTheDocument();
  });

  it('regression: Clear filters never re-applies the stale debounced search', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { calls, router } = setup(() => page([]), { route: '/?q=outage' });
    await screen.findByText('No emails match these filters');
    const seen: string[] = [];
    const unsubscribe = router.subscribe((state) => seen.push(state.location.search));
    const before = listCalls(calls).length;

    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    await act(() => vi.advanceTimersByTimeAsync(1000));
    unsubscribe();

    expect(router.state.location.search).toBe('');
    expect(seen.filter((search) => search.includes('q='))).toEqual([]);
    const after = listCalls(calls).slice(before);
    expect(after.length).toBeGreaterThan(0);
    for (const call of after) expect(queryOf(call).has('q')).toBe(false);
  });

  it('paginates with offset', async () => {
    const user = userEvent.setup();
    const { calls, router } = setup((call) =>
      page([emailListItem()], { total: 60, offset: Number(queryOf(call).get('offset')) }),
    );
    await screen.findByText('1–25 of 60');
    await user.click(screen.getByRole('button', { name: 'Page 2' }));
    await waitFor(() => expect(router.state.location.search).toBe('?offset=25'));
    expect(await screen.findByText('26–50 of 60')).toBeInTheDocument();
    expect(queryOf(listCalls(calls).at(-1)!).get('offset')).toBe('25');
    await user.click(screen.getByRole('button', { name: 'Page 1' }));
    await waitFor(() => expect(router.state.location.search).toBe(''));
  });
});

describe('<InboxPage> sync', () => {
  it('POSTs /emails/sync with {} and refetches the list after 3 s and 8 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { calls } = setup(() => page([emailListItem()]), {
      session: PERSONAS.member,
      withNotifications: true,
      extra: [
        {
          method: 'POST',
          path: '/api/v1/emails/sync',
          handler: () => jsonResponse({ job_ids: ['j-1', 'j-2'], message: 'Sync jobs queued.' }, 202),
        },
      ],
    });
    await screen.findByRole('table', { name: 'Emails' });
    await user.click(screen.getByRole('button', { name: 'Sync mailbox' }));
    expect(await screen.findByText('Mailbox sync queued (2 jobs)')).toBeInTheDocument();
    expect(screen.getByText('Sync jobs queued.')).toBeInTheDocument();
    expect(callsTo(calls, 'POST', '/api/v1/emails/sync')[0]!.body).toEqual({});

    const initial = listCalls(calls).length;
    await act(() => vi.advanceTimersByTimeAsync(2900));
    expect(listCalls(calls)).toHaveLength(initial);
    await act(() => vi.advanceTimersByTimeAsync(200));
    await waitFor(() => expect(listCalls(calls)).toHaveLength(initial + 1));
    await act(() => vi.advanceTimersByTimeAsync(5000));
    await waitFor(() => expect(listCalls(calls)).toHaveLength(initial + 2));
  });

  it('says "Nothing to sync" when no job was queued', async () => {
    const user = userEvent.setup();
    setup(() => page([emailListItem()]), {
      withNotifications: true,
      extra: [
        {
          method: 'POST',
          path: '/api/v1/emails/sync',
          handler: () => jsonResponse({ job_ids: [], message: 'Already up to date.' }, 202),
        },
      ],
    });
    await screen.findByRole('table', { name: 'Emails' });
    await user.click(screen.getByRole('button', { name: 'Sync mailbox' }));
    expect(await screen.findByText('Nothing to sync')).toBeInTheDocument();
  });

  it('shows the sync error with its support reference', async () => {
    const user = userEvent.setup();
    setup(() => page([emailListItem()]), {
      withNotifications: true,
      extra: [
        {
          method: 'POST',
          path: '/api/v1/emails/sync',
          handler: () =>
            errorResponse(503, 'mailbox_unavailable', {
              message: 'The mailbox is unavailable.',
              correlationId: 'corr-sync',
            }),
        },
      ],
    });
    await screen.findByRole('table', { name: 'Emails' });
    await user.click(screen.getByRole('button', { name: 'Sync mailbox' }));
    expect(await screen.findByText('Could not start mailbox sync')).toBeInTheDocument();
    expect(screen.getByText('The mailbox is unavailable.')).toBeInTheDocument();
    expect(screen.getByText('mailbox_unavailable · ref corr-sync')).toBeInTheDocument();
  });
});

describe('<InboxPage> analyze', () => {
  it('POSTs analyze for the clicked row only, showing loading only there', async () => {
    const user = userEvent.setup();
    const hold = deferred<Response>();
    const { calls } = setup(() => page([emailListItem(), notAnalyzed]), {
      withNotifications: true,
      extra: [{ method: 'POST', path: '/api/v1/emails/email-2/analyze', handler: () => hold.promise }],
    });
    const analyze = await screen.findByRole('button', { name: 'Analyze “Invoice question”' });
    await user.click(analyze);
    await waitFor(() => expect(analyze).toHaveAttribute('data-loading', 'true'));
    expect(analyze).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Re-analyze “URGENT: checkout broken”' })).not.toHaveAttribute(
      'data-loading',
    );
    expect(callsTo(calls, 'POST', '/api/v1/emails/email-2/analyze')).toHaveLength(1);
    // Double click while pending does not send a second request.
    await user.click(analyze);
    expect(callsTo(calls, 'POST', '/api/v1/emails/email-2/analyze')).toHaveLength(1);

    hold.resolve(
      jsonResponse({ workflow_run: workflowRun({ id: 'run-2' }), created: true, job_id: 'j' }, 202),
    );
    expect(await screen.findByText('Analysis started')).toBeInTheDocument();
    await waitFor(() => expect(analyze).not.toHaveAttribute('data-loading'));
  });

  it('shows an info toast when the analysis is already running', async () => {
    const user = userEvent.setup();
    setup(() => page([notAnalyzed]), {
      withNotifications: true,
      extra: [
        {
          method: 'POST',
          path: '/api/v1/emails/email-2/analyze',
          handler: () => jsonResponse({ workflow_run: workflowRun(), created: false, job_id: null }),
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: 'Analyze “Invoice question”' }));
    expect(await screen.findByText('Analysis already in progress')).toBeInTheDocument();
  });

  it('disables Analyze while the email is queued or analyzing', async () => {
    setup(() =>
      page([
        emailListItem({ id: 'e-q', subject: 'Queued one', analysis_status: 'queued' }),
        emailListItem({ id: 'e-a', subject: 'Running one', analysis_status: 'analyzing' }),
      ]),
    );
    expect(await screen.findByRole('button', { name: 'Re-analyze “Queued one”' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Re-analyze “Running one”' })).toBeDisabled();
  });
});

describe('<InboxPage> polling', () => {
  it('polls every 2 s while an email is in flight and stops once none is', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status: EmailListItem['analysis_status'] = 'analyzing';
    const { calls } = setup(() => page([emailListItem({ analysis_status: status })]));
    await screen.findByLabelText('Status: Analyzing');
    expect(listCalls(calls)).toHaveLength(1);

    await act(() => vi.advanceTimersByTimeAsync(2000));
    await waitFor(() => expect(listCalls(calls)).toHaveLength(2));

    status = 'awaiting_review';
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(await screen.findByLabelText('Status: Awaiting review')).toBeInTheDocument();
    const settled = listCalls(calls).length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(listCalls(calls)).toHaveLength(settled);
  });

  it('does not poll when nothing is in flight', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = setup(() => page([emailListItem({ analysis_status: 'done' })]));
    await screen.findByLabelText('Status: Done');
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(listCalls(calls)).toHaveLength(1);
  });
});
