import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActionDetail, ApprovalOut, TargetIntegration } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import {
  ALL_ACTION_PERMISSIONS,
  HASH_V3,
  HASH_V4,
  NO_ACTION_PERMISSIONS,
  XSS_IMG,
  XSS_SCRIPT,
  XSS_TEXT,
  actionSummary,
  adoAction,
  approvalFixture,
  auditEvent,
  executionFixture,
  followUpAction,
  noActionAction,
  replyAction,
} from '../../test/fixtures';
import {
  callsTo,
  deferred,
  errorResponse,
  jsonResponse,
  mockFetch,
  renderWithProviders,
  textContent,
  type Route,
} from '../../test/utils';
import { ActionCard } from './ActionCard';

const DETAIL = '/api/v1/actions/act-1';
const approval: ApprovalOut = approvalFixture();

function baseRoutes(detail: () => ActionDetail, history: Route['handler'] = () => jsonResponse([])): Route[] {
  return [
    { method: 'GET', path: DETAIL, handler: () => jsonResponse(detail()) },
    { method: 'GET', path: `${DETAIL}/history`, handler: history },
    {
      method: 'GET',
      path: '/api/v1/integrations/azure-devops/projects',
      handler: () => jsonResponse({ items: [] }),
    },
  ];
}

function renderCard(routes: Route[], props: { focused?: boolean } = {}) {
  const fetch = mockFetch(routes);
  const view = renderWithProviders(<ActionCard actionId="act-1" {...props} />, { withNotifications: true });
  return { ...view, ...fetch };
}

const STALE = () =>
  errorResponse(409, 'stale_version', {
    message: 'The resource was modified. Reload and review the latest version.',
    correlationId: 'corr-stale',
  });

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('<ActionCard> loading and errors', () => {
  it('shows a busy skeleton while loading', async () => {
    const pending = deferred<Response>();
    renderCard([{ method: 'GET', path: DETAIL, handler: () => pending.promise }]);
    expect(screen.getByLabelText('Loading action')).toHaveAttribute('aria-busy', 'true');
    pending.resolve(jsonResponse(adoAction()));
    expect(await screen.findByRole('heading', { name: adoAction().title })).toBeInTheDocument();
  });

  it('shows the error with its support reference and retries on demand', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = renderCard([
      ...baseRoutes(() => adoAction()).slice(1),
      {
        method: 'GET',
        path: DETAIL,
        handler: () =>
          fail
            ? errorResponse(503, 'service_unavailable', { correlationId: 'corr-503' })
            : jsonResponse(adoAction()),
      },
    ]);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('corr-503');
    expect(alert).toHaveTextContent('service_unavailable, HTTP 503');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('heading', { name: adoAction().title })).toBeInTheDocument();
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(2);
  });
});

describe('<ActionCard> header and content', () => {
  it('renders type, target, title, status, version and payload hash (copyable)', async () => {
    const user = userEvent.setup();
    renderCard(baseRoutes(() => adoAction({ version: 7, payload_hash: HASH_V4 })));
    const card = await screen.findByRole('article', { name: adoAction().title });
    expect(card).toHaveTextContent('Azure DevOps work item · Azure DevOps');
    expect(within(card).getByLabelText('Status: Pending approval')).toBeInTheDocument();
    expect(within(card).getByLabelText('Version 7')).toHaveTextContent('v7');
    const hash = within(card).getByRole('button', {
      name: `Payload hash ${HASH_V4.slice(0, 12)}. Copy full hash`,
    });
    await user.click(hash);
    await expect(navigator.clipboard.readText()).resolves.toBe(HASH_V4);
    await expectNoSeriousA11yViolations();
  });

  it.each<[string, ActionDetail, string]>([
    ['reply', replyAction({ id: 'act-1' }), 'Reply draft · Reply drafts (in-app, never sent)'],
    ['follow-up', followUpAction({ id: 'act-1' }), 'Follow-up task · NUVEXA tasks'],
    ['no-action', noActionAction({ id: 'act-1' }), 'No action · None'],
    [
      'unknown target',
      adoAction({ target_integration: 'jira' as TargetIntegration }),
      'Azure DevOps work item · jira',
    ],
  ])('labels the %s action type and target', async (_name, action, label) => {
    renderCard(baseRoutes(() => action));
    expect(await screen.findByText(label)).toBeInTheDocument();
  });

  it('highlights the focused card', async () => {
    renderCard(
      baseRoutes(() => adoAction()),
      { focused: true },
    );
    expect(await screen.findByTestId('action-card-act-1')).toHaveAttribute(
      'style',
      expect.stringContaining('border-width: 2px'),
    );
  });

  it('shows rationale, impact, validation errors/warnings, last error and the external item', async () => {
    renderCard(
      baseRoutes(() =>
        adoAction({
          status: 'completed',
          validation_errors: ['Project "Ops" is not allowed'],
          validation_warnings: ['Deadline not stated'],
          last_error: 'Azure DevOps returned 429',
          external_id: '4711',
          external_url: 'https://dev.azure.com/contoso/Payments/_workitems/edit/4711',
        }),
      ),
    );
    expect(
      await screen.findByText('The sender reports a production outage affecting checkout.'),
    ).toBeInTheDocument();
    expect(screen.getByText('Engineering is notified with the reported facts.')).toBeInTheDocument();
    expect(screen.getByRole('alert', { name: 'Validation errors' })).toHaveTextContent(
      'Project "Ops" is not allowed',
    );
    expect(screen.getByRole('alert', { name: 'Warnings' })).toHaveTextContent('Deadline not stated');
    expect(screen.getByRole('alert', { name: 'Last error' })).toHaveTextContent('Azure DevOps returned 429');
    expect(screen.getByText('#4711')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /Open/ });
    expect(link).toHaveAttribute('href', 'https://dev.azure.com/contoso/Payments/_workitems/edit/4711');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('shows "—" for an empty rationale and omits empty impact/alerts/external row', async () => {
    renderCard(baseRoutes(() => adoAction({ rationale: '', expected_impact: '' })));
    expect(await screen.findByText('Why this action')).toBeInTheDocument();
    expect(screen.getByText('Why this action').nextElementSibling).toHaveTextContent('—');
    expect(screen.queryByText(/Expected impact/)).toBeNull();
    expect(screen.queryByText('External item')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('never links a mock:// external item (text + Mock badge)', async () => {
    renderCard(
      baseRoutes(() =>
        adoAction({
          status: 'completed',
          external_url: 'mock://contoso/Payments/_workitems/edit/9',
          external_id: null,
        }),
      ),
    );
    const text = await screen.findByText('mock://contoso/Payments/_workitems/edit/9');
    expect(text.closest('a')).toBeNull();
    expect(screen.getByText('Mock')).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('a superseded proposal is dimmed, explained and read-only', async () => {
    renderCard(baseRoutes(() => adoAction({ status: 'superseded' })));
    expect(await screen.findByText(/Superseded by a newer analysis of this email/)).toBeInTheDocument();
    expect(screen.getByTestId('action-card-act-1')).toHaveStyle({ opacity: '0.72' });
    expect(screen.getByRole('textbox', { name: /^Title/ })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull();
  });

  it('renders every untrusted string as inert text (no HTML elements are created)', async () => {
    const user = userEvent.setup();
    const { container } = renderCard(
      baseRoutes(() =>
        adoAction({
          status: 'failed',
          title: XSS_TEXT,
          rationale: XSS_IMG,
          expected_impact: XSS_SCRIPT,
          last_error: XSS_IMG,
          validation_errors: [XSS_SCRIPT],
          validation_warnings: [XSS_IMG],
          current_approval: { ...approval, reason: XSS_TEXT },
          approvals: [{ ...approval, reason: XSS_IMG, decided_by_name: XSS_SCRIPT }],
          executions: [executionFixture({ status: 'failed', error_message: XSS_IMG, external_id: null })],
        }),
      ),
    );
    expect(await screen.findByRole('heading', { name: XSS_TEXT })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Approval history/ }));
    await user.click(screen.getByRole('button', { name: /Execution results/ }));
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getAllByText(XSS_IMG).length).toBeGreaterThanOrEqual(4);
    expect(screen.getAllByText(XSS_SCRIPT).length).toBeGreaterThanOrEqual(2);
    expect((window as unknown as { __nuvexaXss?: number }).__nuvexaXss).toBeUndefined();
  });
});

describe('<ActionCard> current approval and history', () => {
  it.each<[string, Partial<ActionDetail>, RegExp[]]>([
    ['none', { current_approval: null }, [/No current approval\./]],
    [
      'valid approval',
      { status: 'approved', current_approval: { ...approval, reason: 'Matches the email' } },
      [/Approved by Avery Approver/, /for version v3/, /Comment: Matches the email/],
    ],
    [
      'rejection by an unnamed reviewer',
      { status: 'rejected', current_approval: { ...approval, decision: 'rejected', decided_by_name: null } },
      [/Rejected by a reviewer/],
    ],
    [
      'invalidated approval',
      {
        current_approval: {
          ...approval,
          invalidated_at: '2026-10-24T10:10:00Z',
          invalidated_reason: 'payload edited',
        },
      },
      [
        /Approval by Avery Approver is no longer valid/,
        /Invalidated \(payload edited\) — the payload changed after approval\. A new approval is required before execution\./,
      ],
    ],
    [
      'invalidated approval without a reason',
      { current_approval: { ...approval, invalidated_at: '2026-10-24T10:10:00Z' } },
      [/Invalidated — the payload changed after approval/],
    ],
    [
      'approval for an older version',
      { version: 4, payload_hash: HASH_V4, current_approval: approval },
      [/is no longer valid/, /It was given for v3; the action is now v4\. A new approval is required/],
    ],
  ])('%s', async (_name, overrides, texts) => {
    renderCard(baseRoutes(() => adoAction(overrides)));
    const card = await screen.findByRole('article', { name: adoAction().title });
    for (const text of texts) expect(card).toHaveTextContent(text);
  });

  it('the approval history strikes through invalidated decisions', async () => {
    const user = userEvent.setup();
    renderCard(
      baseRoutes(() =>
        adoAction({
          approvals: [
            {
              ...approval,
              id: 'ap-1',
              invalidated_at: '2026-10-24T10:10:00Z',
              invalidated_reason: 'payload edited',
            },
            { ...approval, id: 'ap-2', decision: 'rejected', decided_by_name: null, reason: 'Wrong project' },
          ],
        }),
      ),
    );
    await user.click(await screen.findByRole('button', { name: 'Approval history (2)' }));
    const invalidated = screen.getByText(textContent(/Avery Approver · v3/));
    expect(invalidated).toHaveClass('nuvexa-invalidated');
    expect(screen.getByText(/— payload edited/)).toBeInTheDocument();
    expect(screen.getByText(textContent(/Reviewer · v3/))).not.toHaveClass('nuvexa-invalidated');
    expect(screen.getByText('Wrong project')).toBeInTheDocument();
    expect(screen.getByText('Rejected')).toBeInTheDocument();
  });

  it('shows "No decisions yet." and "Not executed yet." for a fresh proposal', async () => {
    const user = userEvent.setup();
    renderCard(baseRoutes(() => adoAction()));
    await user.click(await screen.findByRole('button', { name: 'Approval history (0)' }));
    await user.click(screen.getByRole('button', { name: 'Execution results (0)' }));
    expect(screen.getByText('No decisions yet.')).toBeInTheDocument();
    expect(screen.getByText('Not executed yet.')).toBeInTheDocument();
  });
});

describe('<ActionCard> execution results, audit trail and raw payload', () => {
  it('lists execution attempts with dedup, external links (https vs mock://) and errors', async () => {
    const user = userEvent.setup();
    renderCard(
      baseRoutes(() =>
        adoAction({
          status: 'completed',
          executions: [
            executionFixture({ id: 'ex-1', deduplicated: true, retry_count: 2, latency_ms: 2100 }),
            executionFixture({
              id: 'ex-2',
              attempt_number: 2,
              external_url: 'mock://contoso/Payments/_workitems/edit/4712',
              external_id: '4712',
            }),
            executionFixture({
              id: 'ex-3',
              attempt_number: 3,
              status: 'failed',
              external_id: null,
              external_url: null,
              error_category: 'rate_limited',
              error_message: 'Too many requests',
              provider_status_code: 429,
            }),
            executionFixture({
              id: 'ex-4',
              attempt_number: 4,
              status: 'uncertain',
              external_id: null,
              external_url: null,
              error_category: null,
              error_message: 'Connection reset',
              provider_status_code: null,
            }),
          ],
        }),
      ),
    );
    await user.click(await screen.findByRole('button', { name: 'Execution results (4)' }));
    // The accordion panel becomes visible after Mantine's collapse transition (two animation frames).
    const table = await screen.findByRole('table', { name: 'Execution attempts' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(4);
    expect(rows[0]).toHaveTextContent('#1 · v3');
    expect(within(rows[0]!).getByText('Deduplicated')).toBeInTheDocument();
    expect(within(rows[0]!).getByLabelText('Status: Succeeded')).toBeInTheDocument();
    expect(rows[0]).toHaveTextContent('2.10 s');
    const link = within(rows[0]!).getByRole('link', { name: /Open work item/ });
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(link).toHaveAttribute('target', '_blank');
    expect(within(rows[1]!).queryByRole('link')).toBeNull();
    expect(within(rows[1]!).getByText('mock://contoso/Payments/_workitems/edit/4712')).toBeInTheDocument();
    expect(within(rows[1]!).getByText('Mock')).toBeInTheDocument();
    expect(rows[2]).toHaveTextContent('Rate limited: Too many requests (HTTP 429)');
    expect(within(rows[2]!).getByLabelText('Status: Failed')).toBeInTheDocument();
    expect(rows[3]).toHaveTextContent('Connection reset');
    expect(within(rows[3]!).getByLabelText('Status: Uncertain')).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
  });

  it('loads the audit trail lazily: skeleton → events (actor fallback) ', async () => {
    const user = userEvent.setup();
    const history = deferred<Response>();
    renderCard(
      baseRoutes(
        () => adoAction(),
        () => history.promise,
      ),
    );
    await user.click(await screen.findByRole('button', { name: 'Audit trail' }));
    history.resolve(
      jsonResponse([
        auditEvent({ id: 'au-1', event_type: 'action.approved' }),
        auditEvent({
          id: 'au-2',
          event_type: 'action.proposed',
          actor_name: null,
          actor_type: 'agent',
          outcome: 'success',
        }),
        auditEvent({ id: 'au-3', event_type: 'action.execute_denied', outcome: 'denied' }),
      ]),
    );
    expect(await screen.findByText('Action approved')).toBeInTheDocument();
    expect(screen.getByText('Action proposed')).toBeInTheDocument();
    expect(screen.getByText(textContent(/^Agent ·/))).toBeInTheDocument();
    expect(screen.getByLabelText('Status: Denied')).toBeInTheDocument();
  });

  it('audit trail: empty and error (with retry) states', async () => {
    const user = userEvent.setup();
    let fail = true;
    renderCard(
      baseRoutes(
        () => adoAction(),
        () =>
          fail ? errorResponse(500, 'internal_error', { correlationId: 'corr-hist' }) : jsonResponse([]),
      ),
    );
    await user.click(await screen.findByRole('button', { name: 'Audit trail' }));
    expect(await screen.findByText('corr-hist')).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('No audit events.')).toBeInTheDocument();
  });

  it('shows the idempotency key, the full payload hash (copy) and the raw JSON', async () => {
    const user = userEvent.setup();
    renderCard(baseRoutes(() => adoAction()));
    await user.click(await screen.findByRole('button', { name: 'Raw payload & identifiers' }));
    expect(screen.getByText('f'.repeat(64))).toBeInTheDocument();
    expect(screen.getByText(HASH_V3)).toBeInTheDocument();
    expect(screen.getByText(/"project": "Payments"/)).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: 'Copy' }));
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument();
    await expect(navigator.clipboard.readText()).resolves.toBe(HASH_V3);
  });
});

describe('<ActionCard> approve flow', () => {
  it('disables while pending, never updates the status optimistically, and sends version + payload_hash', async () => {
    const user = userEvent.setup();
    const pendingApprove = deferred<Response>();
    const { calls } = renderCard([
      ...baseRoutes(() => adoAction()),
      { method: 'POST', path: `${DETAIL}/approve`, handler: () => pendingApprove.promise },
    ]);

    expect(
      await screen.findByText('Create bug: Checkout fails with 500 for EU customers'),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Status: Pending approval')).toBeInTheDocument();
    expect(screen.getByLabelText('Version 3')).toBeInTheDocument();

    await user.click(screen.getByTestId('approve-button'));
    await user.type(await screen.findByLabelText(/Comment \(optional\)/), 'Matches the email');
    const confirm = screen.getByTestId('confirm-approve');
    await user.click(confirm);

    // Request is in flight: buttons are disabled with a loading state…
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(confirm).toHaveAttribute('data-loading');
    expect(screen.getByTestId('approve-button')).toBeDisabled();
    // …and the status is NOT changed optimistically.
    expect(screen.getByLabelText('Status: Pending approval')).toBeInTheDocument();
    expect(screen.queryByLabelText('Status: Approved')).toBeNull();
    expect(screen.queryByTestId('execute-button')).toBeNull();

    const approveCall = calls.find((c) => c.method === 'POST' && c.url === `${DETAIL}/approve`);
    expect(approveCall?.body).toEqual({ version: 3, payload_hash: HASH_V3, comment: 'Matches the email' });

    // Server confirms → the confirmed status is shown.
    pendingApprove.resolve(
      jsonResponse(adoAction({ status: 'approved', current_approval: approval, approvals: [approval] })),
    );
    expect(await screen.findByLabelText('Status: Approved')).toBeInTheDocument();
    expect(screen.queryByLabelText('Status: Pending approval')).toBeNull();
    expect(screen.getByTestId('execute-button')).toBeEnabled();
    expect(screen.getByText(textContent('Approved by Avery Approver'))).toBeInTheDocument();
  });

  it('sends exactly the displayed (non-default) version + payload hash', async () => {
    const user = userEvent.setup();
    const shown = adoAction({ version: 7, payload_hash: HASH_V4 });
    const { calls } = renderCard([
      ...baseRoutes(() => shown),
      {
        method: 'POST',
        path: `${DETAIL}/approve`,
        handler: () => jsonResponse({ ...shown, status: 'approved' }),
      },
    ]);
    expect(await screen.findByLabelText('Version 7')).toBeInTheDocument();
    expect(screen.getByTestId('payload-hash')).toHaveTextContent(HASH_V4.slice(0, 12));
    await user.click(screen.getByTestId('approve-button'));
    await user.click(await screen.findByTestId('confirm-approve'));
    await screen.findByLabelText('Status: Approved');
    expect(callsTo(calls, 'POST', `${DETAIL}/approve`)).toHaveLength(1);
    expect(callsTo(calls, 'POST', `${DETAIL}/approve`)[0]!.body).toEqual({
      version: 7,
      payload_hash: HASH_V4,
      comment: null,
    });
  });

  it('blocks approval while there are unsaved edits', async () => {
    const user = userEvent.setup();
    mockFetch(baseRoutes(() => adoAction()));
    renderWithProviders(<ActionCard actionId="act-1" />);
    const title = await screen.findByRole('textbox', { name: /^Title/ });
    await user.type(title, '!');
    expect(screen.getByTestId('approve-button')).toBeDisabled();
    expect(screen.getByText(/Save or discard your edits first/)).toBeInTheDocument();
  });
});

describe('<ActionCard> reject / cancel are never optimistic', () => {
  it('reject: badge unchanged while pending, then the confirmed "Rejected"', async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    renderCard([
      ...baseRoutes(() => adoAction()),
      { method: 'POST', path: `${DETAIL}/reject`, handler: () => pending.promise },
    ]);
    await user.click(await screen.findByRole('button', { name: 'Reject' }));
    await user.type(screen.getByRole('textbox', { name: /Reason/ }), 'Duplicate');
    const confirm = screen.getByRole('button', { name: 'Reject proposal' });
    await user.click(confirm);
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(screen.getByLabelText('Status: Pending approval')).toBeInTheDocument();
    expect(screen.queryByLabelText('Status: Rejected')).toBeNull();
    pending.resolve(
      jsonResponse(
        adoAction({
          status: 'rejected',
          current_approval: { ...approval, decision: 'rejected', reason: 'Duplicate' },
        }),
      ),
    );
    expect(await screen.findByLabelText('Status: Rejected')).toBeInTheDocument();
    expect(screen.getByText('No actions available for you in this state.')).toBeInTheDocument();
  });

  it('cancel: badge unchanged while pending, then the confirmed "Cancelled"', async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    renderCard([
      ...baseRoutes(() => adoAction()),
      { method: 'POST', path: `${DETAIL}/cancel`, handler: () => pending.promise },
    ]);
    await user.click(await screen.findByRole('button', { name: 'Cancel proposal' }));
    const confirm = within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel proposal' });
    await user.click(confirm);
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(screen.getByLabelText('Status: Pending approval')).toBeInTheDocument();
    pending.resolve(jsonResponse(adoAction({ status: 'cancelled' })));
    expect(await screen.findByLabelText('Status: Cancelled')).toBeInTheDocument();
  });
});

describe('<ActionCard> save (PATCH) and approval invalidation', () => {
  it('PATCHes with expected_version and makes the invalidated approval obvious', async () => {
    const user = userEvent.setup();
    const approvedV3 = adoAction({ status: 'approved', current_approval: approval, approvals: [approval] });
    const { calls } = renderCard([
      ...baseRoutes(() => approvedV3),
      {
        method: 'PATCH',
        path: DETAIL,
        handler: (call) => {
          const body = call.body as { payload: Record<string, unknown> };
          return jsonResponse(
            adoAction({
              status: 'pending_approval',
              version: 4,
              payload_hash: HASH_V4,
              payload: body.payload,
              current_approval: {
                ...approval,
                invalidated_at: '2026-10-24T10:10:00Z',
                invalidated_reason: 'payload edited',
              },
              approvals: [
                { ...approval, invalidated_at: '2026-10-24T10:10:00Z', invalidated_reason: 'payload edited' },
              ],
            }),
          );
        },
      },
    ]);
    expect(await screen.findByLabelText('Status: Approved')).toBeInTheDocument();
    expect(screen.getByTestId('execute-button')).toBeEnabled();

    const title = screen.getByRole('textbox', { name: /^Title/ });
    await user.clear(title);
    await user.click(title);
    await user.paste('Checkout returns HTTP 500 (EU)');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    const patch = await waitFor(() => {
      const call = callsTo(calls, 'PATCH', DETAIL)[0];
      expect(call).toBeDefined();
      return call!;
    });
    const body = patch.body as { expected_version: number; payload: Record<string, unknown> };
    expect(body.expected_version).toBe(3);
    expect(body.payload).toMatchObject({
      kind: 'create_azure_devops_work_item',
      title: 'Checkout returns HTTP 500 (EU)',
      priority: 1,
      source_email: { email_id: 'email-1' },
    });

    const notice = await screen.findByTestId('save-notice');
    expect(notice).toHaveTextContent('Saved as version v4');
    expect(notice).toHaveTextContent(`New payload hash ${HASH_V4.slice(0, 12)}`);
    expect(notice).toHaveTextContent(
      /previous approval by Avery Approver \(v3\) was invalidated — this version must be approved again/,
    );
    expect(screen.getByLabelText('Status: Pending approval')).toBeInTheDocument();
    expect(screen.getByLabelText('Version 4')).toBeInTheDocument();
    expect(screen.getByTestId('payload-hash')).toHaveTextContent(HASH_V4.slice(0, 12));
    expect(
      screen.getByText(textContent('Approval by Avery Approver is no longer valid')),
    ).toBeInTheDocument();
    // Execute is gone until the new version is approved.
    expect(screen.queryByTestId('execute-button')).toBeNull();
    expect(screen.getByTestId('approve-button')).toBeEnabled();

    await user.click(screen.getByRole('button', { name: 'Approval history (1)' }));
    expect(screen.getByText(textContent(/Avery Approver · v3/))).toHaveClass('nuvexa-invalidated');

    await user.click(within(notice).getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByTestId('save-notice')).toBeNull();
  });

  it('without a prior approval the notice explains that approvals bind to the saved version', async () => {
    const user = userEvent.setup();
    renderCard([
      ...baseRoutes(() => adoAction()),
      {
        method: 'PATCH',
        path: DETAIL,
        handler: () => jsonResponse(adoAction({ version: 4, payload_hash: HASH_V4 })),
      },
    ]);
    await user.type(await screen.findByRole('textbox', { name: /^Title/ }), '!');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    const notice = await screen.findByTestId('save-notice');
    expect(notice).toHaveTextContent('Approvals always bind to the saved version');
  });
});

describe('<ActionCard> 409 stale_version → reload banner', () => {
  it('approve: shows the banner with the support ref, keeps the old status and reloads on demand', async () => {
    const user = userEvent.setup();
    let detail = adoAction();
    const { calls } = renderCard([
      ...baseRoutes(() => detail),
      {
        method: 'POST',
        path: `${DETAIL}/approve`,
        handler: () => {
          detail = adoAction({ version: 5, payload_hash: HASH_V4 });
          return STALE();
        },
      },
    ]);
    await user.click(await screen.findByTestId('approve-button'));
    await user.click(await screen.findByTestId('confirm-approve'));

    const banner = await screen.findByRole('alert', {
      name: 'This action changed — reload before continuing',
    });
    expect(within(banner).getByText('ref corr-stale')).toBeInTheDocument();
    expect(screen.getByLabelText('Status: Pending approval')).toBeInTheDocument();
    // The editor is locked while the conflict is unresolved.
    expect(screen.getByRole('textbox', { name: /^Title/ })).toBeDisabled();

    const getsBefore = callsTo(calls, 'GET', DETAIL).length;
    await user.click(within(banner).getByRole('button', { name: 'Reload action' }));
    expect(await screen.findByLabelText('Version 5')).toBeInTheDocument();
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(getsBefore + 1);
    await waitFor(() =>
      expect(screen.queryByText('This action changed — reload before continuing')).toBeNull(),
    );
    expect(screen.getByRole('textbox', { name: /^Title/ })).toBeEnabled();
  });

  it.each<
    [string, Partial<ActionDetail>, (user: ReturnType<typeof userEvent.setup>) => Promise<void>, Route]
  >([
    [
      'reject',
      {},
      async (user) => {
        await user.click(screen.getByRole('button', { name: 'Reject' }));
        await user.type(screen.getByRole('textbox', { name: /Reason/ }), 'no');
        await user.click(screen.getByRole('button', { name: 'Reject proposal' }));
      },
      { method: 'POST', path: `${DETAIL}/reject`, handler: STALE },
    ],
    [
      'cancel',
      {},
      async (user) => {
        await user.click(screen.getByRole('button', { name: 'Cancel proposal' }));
        await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel proposal' }));
      },
      { method: 'POST', path: `${DETAIL}/cancel`, handler: STALE },
    ],
    [
      'execute',
      { status: 'approved', current_approval: approval },
      async (user) => {
        await user.click(screen.getByTestId('execute-button'));
        await user.click(screen.getByTestId('confirm-execute'));
      },
      { method: 'POST', path: `${DETAIL}/execute`, handler: STALE },
    ],
    [
      'reconcile',
      { status: 'uncertain', permissions: ALL_ACTION_PERMISSIONS },
      async (user) => {
        await user.click(screen.getByRole('button', { name: 'Reconcile' }));
      },
      { method: 'POST', path: `${DETAIL}/reconcile`, handler: STALE },
    ],
    [
      'save',
      {},
      async (user) => {
        await user.type(screen.getByRole('textbox', { name: /^Title/ }), '!');
        await user.click(screen.getByRole('button', { name: 'Save changes' }));
      },
      { method: 'PATCH', path: DETAIL, handler: STALE },
    ],
  ])('%s', async (_name, overrides, act_, route) => {
    const user = userEvent.setup();
    renderCard([...baseRoutes(() => adoAction(overrides)), route]);
    await screen.findByRole('heading', { name: adoAction().title });
    await act_(user);
    const banner = await screen.findByRole('alert', {
      name: 'This action changed — reload before continuing',
    });
    expect(banner).toHaveTextContent('ref corr-stale');
    expect(within(banner).getByRole('button', { name: 'Reload action' })).toBeInTheDocument();
    // No error toast for a stale version: the banner is the single signal.
    expect(screen.queryByText(/failed/i)).toBeNull();
  });
});

describe('<ActionCard> execute / reconcile (queued jobs, polling with fake timers)', () => {
  it('execute: queued (no optimistic status), polls every 2 s, then shows the confirmed result; mock:// stays text', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const approved = adoAction({ status: 'approved', current_approval: approval, approvals: [approval] });
    const executing = { ...approved, status: 'executing' as const, updated_at: '2026-10-24T10:20:00Z' };
    const completed = {
      ...approved,
      status: 'completed' as const,
      updated_at: '2026-10-24T10:20:05Z',
      external_id: '4711',
      external_url: 'mock://contoso/Payments/_workitems/edit/4711',
      executions: [
        executionFixture({ retry_count: 1, external_url: 'mock://contoso/Payments/_workitems/edit/4711' }),
      ],
    };
    let state: ActionDetail = approved;
    const pendingExecute = deferred<Response>();
    const { calls } = renderCard([
      ...baseRoutes(() => state),
      { method: 'POST', path: `${DETAIL}/execute`, handler: () => pendingExecute.promise },
    ]);
    await user.click(await screen.findByTestId('execute-button'));
    await user.click(await screen.findByTestId('confirm-execute'));
    await waitFor(() => expect(screen.getByTestId('confirm-execute')).toBeDisabled());
    expect(screen.getByLabelText('Status: Approved')).toBeInTheDocument();

    expect(callsTo(calls, 'POST', `${DETAIL}/execute`)[0]!.body).toEqual({
      version: 3,
      payload_hash: HASH_V3,
    });
    // 202: the job is queued; the action is still "approved" until a worker picks it up.
    pendingExecute.resolve(jsonResponse({ action: actionSummary({ ...approved }), job_id: 'job-1' }, 202));
    expect(await screen.findByText(/Queued — waiting for a worker/)).toBeInTheDocument();
    expect(screen.getByLabelText('Status: Approved')).toBeInTheDocument();
    expect(screen.getByTestId('execute-button')).toBeDisabled();

    const before = callsTo(calls, 'GET', DETAIL).length;
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(callsTo(calls, 'GET', DETAIL).length).toBeGreaterThan(before);

    state = executing;
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(await screen.findByLabelText('Status: Executing')).toBeInTheDocument();
    expect(screen.queryByText(/Queued — waiting for a worker/)).toBeNull();

    state = completed;
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(await screen.findByLabelText('Status: Completed')).toBeInTheDocument();
    const mockUrl = screen.getAllByText('mock://contoso/Payments/_workitems/edit/4711');
    expect(mockUrl.length).toBeGreaterThan(0);
    for (const node of mockUrl) expect(node.closest('a')).toBeNull();
    expect(screen.getAllByText('Mock').length).toBeGreaterThan(0);

    // Terminal: polling stops.
    const settled = callsTo(calls, 'GET', DETAIL).length;
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(settled);
  });

  it('a queued job that is never picked up stops polling after 120 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const approved = adoAction({ status: 'approved', current_approval: approval });
    const { calls } = renderCard([
      ...baseRoutes(() => approved),
      {
        method: 'POST',
        path: `${DETAIL}/execute`,
        handler: () => jsonResponse({ action: actionSummary({ ...approved }), job_id: 'job-1' }, 202),
      },
    ]);
    await user.click(await screen.findByTestId('execute-button'));
    await user.click(await screen.findByTestId('confirm-execute'));
    expect(await screen.findByText(/Queued — waiting for a worker/)).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    const midway = callsTo(calls, 'GET', DETAIL).length;
    expect(midway).toBeGreaterThan(20);
    await act(() => vi.advanceTimersByTimeAsync(62_000));
    const atDeadline = callsTo(calls, 'GET', DETAIL).length;
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(atDeadline);
  });

  it('after the watch expires it says polling stopped, re-enables the buttons, and a new execute re-arms it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const approved = adoAction({
      status: 'approved',
      current_approval: approval,
      permissions: ALL_ACTION_PERMISSIONS,
    });
    const { calls } = renderCard([
      ...baseRoutes(() => approved),
      {
        method: 'POST',
        path: `${DETAIL}/execute`,
        handler: () => jsonResponse({ action: actionSummary({ ...approved }), job_id: 'job-1' }, 202),
      },
    ]);
    await user.click(await screen.findByTestId('execute-button'));
    await user.click(await screen.findByTestId('confirm-execute'));
    expect(await screen.findByText(/Queued — waiting for a worker/)).toBeInTheDocument();
    expect(screen.queryByText(/Still queued/)).toBeNull();

    await act(() => vi.advanceTimersByTimeAsync(121_000));
    expect(screen.queryByText(/Queued — waiting for a worker/)).toBeNull();
    expect(screen.getByText(/^Still queued/).closest('[role="status"]')).toHaveTextContent(
      'Still queued — no worker has picked up the job yet and this page has stopped checking. Reload to check the latest status.',
    );
    // The server stays the authority: a repeated execute is absorbed by its idempotency checks.
    expect(screen.getByTestId('execute-button')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled();

    // Queueing again re-arms the watch: live message, buttons disabled, polling resumes.
    await user.click(screen.getByTestId('execute-button'));
    await user.click(await screen.findByTestId('confirm-execute'));
    expect(await screen.findByText(/Queued — waiting for a worker/)).toBeInTheDocument();
    expect(screen.queryByText(/Still queued/)).toBeNull();
    expect(screen.getByTestId('execute-button')).toBeDisabled();
    expect(callsTo(calls, 'POST', `${DETAIL}/execute`)).toHaveLength(2);
    const rearmed = callsTo(calls, 'GET', DETAIL).length;
    await act(() => vi.advanceTimersByTimeAsync(4000));
    expect(callsTo(calls, 'GET', DETAIL).length).toBeGreaterThan(rearmed);
  });

  it('reconcile: status stays "Uncertain" until the server resolves it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const uncertain = adoAction({ status: 'uncertain', permissions: ALL_ACTION_PERMISSIONS });
    let state: ActionDetail = uncertain;
    const pending = deferred<Response>();
    renderCard([
      ...baseRoutes(() => state),
      { method: 'POST', path: `${DETAIL}/reconcile`, handler: () => pending.promise },
    ]);
    const reconcile = await screen.findByRole('button', { name: 'Reconcile' });
    await user.click(reconcile);
    await waitFor(() => expect(reconcile).toBeDisabled());
    expect(screen.getByLabelText('Status: Uncertain')).toBeInTheDocument();
    pending.resolve(jsonResponse({ action: actionSummary({ ...uncertain }), job_id: 'job-2' }, 202));
    expect(await screen.findByText(/Queued — waiting for a worker/)).toBeInTheDocument();
    expect(screen.getByLabelText('Status: Uncertain')).toBeInTheDocument();

    state = { ...uncertain, status: 'completed', updated_at: '2026-10-24T11:00:00Z', external_id: '4711' };
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(await screen.findByLabelText('Status: Completed')).toBeInTheDocument();
    expect(screen.queryByText(/Queued — waiting for a worker/)).toBeNull();
  });
});

describe('<ActionCard> permissions', () => {
  it('hides controls the API does not permit', async () => {
    mockFetch(baseRoutes(() => adoAction({ permissions: NO_ACTION_PERMISSIONS })));
    renderWithProviders(<ActionCard actionId="act-1" />);
    expect(await screen.findByText('No actions available for you in this state.')).toBeInTheDocument();
    expect(screen.queryByTestId('approve-button')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull();
    expect(screen.getByRole('textbox', { name: /^Title/ })).toBeDisabled();
  });

  it('can_edit only applies to open proposals (approved stays editable, completed does not)', async () => {
    const { unmount } = renderCard(
      baseRoutes(() =>
        adoAction({ status: 'approved', current_approval: approval, permissions: ALL_ACTION_PERMISSIONS }),
      ),
    );
    expect(await screen.findByRole('textbox', { name: /^Title/ })).toBeEnabled();
    unmount();
    vi.unstubAllGlobals();
    renderCard(baseRoutes(() => adoAction({ status: 'completed', permissions: ALL_ACTION_PERMISSIONS })));
    expect(await screen.findByRole('textbox', { name: /^Title/ })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull();
  });
});
