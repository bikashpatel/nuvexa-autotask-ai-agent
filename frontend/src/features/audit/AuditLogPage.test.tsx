import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AuditEventOut, Page } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { PERSONAS, XSS_IMG, auditEvent, page } from '../../test/fixtures';
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
import { AuditLogPage } from './AuditLogPage';

const LIST = '/api/v1/audit-events';
const ACTION_ID = '7f3c9a10-1111-4c2e-9d8a-0123456789ab';
const RUN_ID = '5b2e8d44-2222-4c2e-9d8a-0123456789ab';
const RESOURCE_ID = 'c0ffee00-3333-4c2e-9d8a-0123456789ab';

type Reply = Response | Promise<Response>;

function setup(
  list: (call: RecordedCall) => Page<AuditEventOut> | Reply = () => page([auditEvent()]),
  options: RenderOptions = {},
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
  ]);
  return { ...mock, ...renderWithProviders(<AuditLogPage />, options) };
}

const listCalls = (calls: RecordedCall[]) => callsTo(calls, 'GET', LIST);
const lastParams = (calls: RecordedCall[]) => queryOf(listCalls(calls).at(-1)!);

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('<AuditLogPage> states', () => {
  it('shows a busy skeleton while loading', () => {
    const hold = deferred<Response>();
    setup(() => hold.promise);
    expect(screen.getByLabelText('Loading audit events')).toHaveAttribute('aria-busy', 'true');
  });

  it('shows the error with its correlation id and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = setup(() =>
      fail ? errorResponse(500, 'internal_error', { correlationId: 'corr-audit' }) : page([auditEvent()]),
    );
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('corr-audit')).toBeInTheDocument();
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('table', { name: 'Audit events' })).toBeInTheDocument();
    expect(listCalls(calls)).toHaveLength(2);
  });

  it('distinguishes an empty log from no matches', async () => {
    const { unmount } = setup(() => page([]));
    expect(await screen.findByText('No audit events yet')).toBeInTheDocument();
    unmount();
    setup(() => page([]), { route: '/?outcome=denied' });
    expect(await screen.findByText('No audit events match these filters')).toBeInTheDocument();
  });

  it('describes the tenant-wide scope only with audit:read_all', async () => {
    const { unmount } = setup(() => page([]), { session: PERSONAS.approver });
    expect(await screen.findByText(/across the tenant/)).toBeInTheDocument();
    unmount();
    setup(() => page([]), { session: PERSONAS.member });
    expect(await screen.findByText('Audit events for your own resources.')).toBeInTheDocument();
  });
});

describe('<AuditLogPage> rows', () => {
  const events = [
    auditEvent({ id: 'au-1', action_id: ACTION_ID, workflow_run_id: RUN_ID }),
    auditEvent({
      id: 'au-2',
      event_type: 'workflow.failed',
      actor_name: null,
      actor_type: 'system',
      resource_type: 'workflow_run',
      action_id: null,
      workflow_run_id: RUN_ID,
      outcome: 'failure',
    }),
    auditEvent({
      id: 'au-3',
      event_type: 'permission.denied',
      actor_type: 'agent',
      actor_name: null,
      resource_type: 'settings',
      action_id: null,
      workflow_run_id: null,
      resource_id: RESOURCE_ID,
      outcome: 'denied',
      correlation_id: null,
    }),
    auditEvent({
      id: 'au-4',
      event_type: 'session.created',
      resource_type: 'session',
      action_id: null,
      workflow_run_id: null,
      resource_id: null,
    }),
  ];

  it('renders events with actor, resource links and outcome badges', async () => {
    const { container } = setup(() => page(events));
    const table = await screen.findByRole('table', { name: 'Audit events' });
    const rows = within(table)
      .getAllByRole('row')
      .filter((row) => within(row).queryByRole('button'));
    expect(rows).toHaveLength(4);
    const [approved, failed, denied, session] = rows.map((r) => within(r));

    expect(approved!.getByText('Action approved')).toBeInTheDocument();
    expect(approved!.getByText('action.approved')).toBeInTheDocument();
    expect(approved!.getByText('Avery Approver')).toBeInTheDocument();
    expect(approved!.getByRole('link', { name: 'action 7f3c9a10' })).toHaveAttribute(
      'href',
      `/actions/${ACTION_ID}`,
    );
    expect(approved!.getByLabelText('Status: Success')).toBeInTheDocument();

    expect(failed!.getAllByText('System')).toHaveLength(2);
    expect(failed!.getByText('Workflow run')).toBeInTheDocument();
    expect(failed!.getByRole('link', { name: 'run 5b2e8d44' })).toHaveAttribute(
      'href',
      `/workflows/${RUN_ID}`,
    );
    expect(failed!.getByLabelText('Status: Failure')).toBeInTheDocument();

    expect(denied!.getAllByText('Agent')).toHaveLength(2);
    expect(denied!.getByText('c0ffee00')).toBeInTheDocument();
    expect(denied!.queryByRole('link')).toBeNull();
    expect(denied!.getByLabelText('Status: Denied')).toBeInTheDocument();

    expect(session!.queryByRole('link')).toBeNull();
    await expectNoSeriousA11yViolations(container);
  });

  it('expands details with an accessible disclosure button (mouse and keyboard)', async () => {
    const user = userEvent.setup();
    setup(() =>
      page([auditEvent({ resource_id: RESOURCE_ID, details: { version: 3, comment: 'Matches the email' } })]),
    );
    const toggle = await screen.findByRole('button', { name: 'Show details for Action approved' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', 'audit-details-au-1');
    const details = document.getElementById('audit-details-au-1')!;
    // The details row is hidden while collapsed (the Collapse height animation itself needs a real
    // browser: jsdom never fires transitionend).
    const detailsRow = details.closest('tr')!;
    expect(detailsRow).not.toBeVisible();

    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toHaveAccessibleName('Hide details for Action approved');
    expect(detailsRow.style.display).toBe('');
    expect(within(details).getByText('corr-audit-1')).toBeInTheDocument();
    expect(within(details).getByText(RESOURCE_ID)).toBeInTheDocument();
    expect(details).toHaveTextContent('"comment": "Matches the email"');
    expect(details).toHaveTextContent(/Oct 24, 2026/);

    toggle.focus();
    await user.keyboard('{Enter}');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(detailsRow).not.toBeVisible();
    await user.keyboard(' ');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
  });

  it('shows dashes for a missing correlation id / resource and renders hostile details as text', async () => {
    const user = userEvent.setup();
    const { container } = setup(() =>
      page([auditEvent({ correlation_id: null, resource_id: null, details: { note: XSS_IMG } })]),
    );
    await user.click(await screen.findByRole('button', { name: /Show details/ }));
    const details = document.getElementById('audit-details-au-1')!;
    expect(within(details).getAllByText('—')).toHaveLength(2);
    expect(details.textContent).toContain('window.__nuvexaXss=1');
    expect(container.querySelector('img')).toBeNull();
  });
});

describe('<AuditLogPage> filters and pagination', () => {
  it('debounces the event type into the URL and the request, resetting the offset', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { calls, router } = setup(() => page([auditEvent()]), { route: '/?offset=50' });
    await screen.findByRole('table', { name: 'Audit events' });
    const input = screen.getByRole('textbox', { name: 'Event type' });
    await user.type(input, ' action.approved ');
    expect(router.state.location.search).toBe('?offset=50');
    await act(() => vi.advanceTimersByTimeAsync(400));
    expect(router.state.location.search).toBe('?event_type=action.approved');
    await waitFor(() => expect(lastParams(calls).get('event_type')).toBe('action.approved'));
    expect(lastParams(calls).get('offset')).toBe('0');

    await user.clear(input);
    await act(() => vi.advanceTimersByTimeAsync(400));
    expect(router.state.location.search).toBe('');
  });

  it('filters by resource type and outcome', async () => {
    const user = userEvent.setup();
    const { calls, router } = setup(() => page([auditEvent()]));
    await screen.findByRole('table', { name: 'Audit events' });

    await user.click(screen.getByRole('combobox', { name: 'Resource type' }));
    await user.click(screen.getByRole('option', { name: 'Workflow run' }));
    await waitFor(() => expect(router.state.location.search).toBe('?resource_type=workflow_run'));
    await waitFor(() => expect(lastParams(calls).get('resource_type')).toBe('workflow_run'));

    await user.click(screen.getByRole('combobox', { name: 'Outcome' }));
    await user.click(screen.getByRole('option', { name: 'Denied' }));
    await waitFor(() => expect(lastParams(calls).get('outcome')).toBe('denied'));
    expect(new URLSearchParams(router.state.location.search).get('outcome')).toBe('denied');
  });

  it('keeps an unknown resource type from the URL selectable and ignores an invalid outcome', async () => {
    const user = userEvent.setup();
    const { calls } = setup(() => page([auditEvent()]), {
      route: '/?resource_type=webhook&outcome=maybe&event_type=webhook.received',
    });
    await screen.findByRole('table', { name: 'Audit events' });
    const params = queryOf(listCalls(calls)[0]!);
    expect(params.get('resource_type')).toBe('webhook');
    expect(params.get('outcome')).toBeNull();
    expect(params.get('event_type')).toBe('webhook.received');
    expect(params.get('limit')).toBe('50');
    expect(screen.getByRole('textbox', { name: 'Event type' })).toHaveValue('webhook.received');
    expect(screen.getByRole('combobox', { name: 'Resource type' })).toHaveValue('Webhook');
    await user.click(screen.getByRole('combobox', { name: 'Resource type' }));
    expect(screen.getByRole('option', { name: 'Webhook' })).toBeInTheDocument();
  });

  it('clears every filter including the text box', async () => {
    const user = userEvent.setup();
    const { router } = setup(() => page([]), { route: '/?event_type=x&outcome=success&resource_type=task' });
    await user.click(await screen.findByRole('button', { name: 'Clear filters' }));
    await waitFor(() => expect(router.state.location.search).toBe(''));
    expect(screen.getByRole('textbox', { name: 'Event type' })).toHaveValue('');
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull();
  });

  it('regression: Clear filters never re-applies the stale debounced event type', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { calls, router } = setup(() => page([]), { route: '/?event_type=action.approved' });
    await screen.findByText('No audit events match these filters');
    const seen: string[] = [];
    const unsubscribe = router.subscribe((state) => seen.push(state.location.search));
    const before = listCalls(calls).length;

    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    await act(() => vi.advanceTimersByTimeAsync(1000));
    unsubscribe();

    expect(router.state.location.search).toBe('');
    expect(seen.filter((search) => search.includes('event_type'))).toEqual([]);
    const after = listCalls(calls).slice(before);
    expect(after.length).toBeGreaterThan(0);
    for (const call of after) expect(queryOf(call).has('event_type')).toBe(false);
  });

  it('paginates by 50 with offset', async () => {
    const user = userEvent.setup();
    const { calls, router } = setup((call) =>
      page([auditEvent()], { total: 120, limit: 50, offset: Number(queryOf(call).get('offset')) }),
    );
    await screen.findByText('1–50 of 120');
    await user.click(screen.getByRole('button', { name: 'Page 2' }));
    await waitFor(() => expect(router.state.location.search).toBe('?offset=50'));
    expect(await screen.findByText('51–100 of 120')).toBeInTheDocument();
    expect(lastParams(calls).get('offset')).toBe('50');
    await user.click(screen.getByRole('button', { name: 'Page 1' }));
    await waitFor(() => expect(router.state.location.search).toBe(''));
  });
});
