import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActionDetail, ApprovalOut } from '../../api/types';
import { HASH_V3, HASH_V4, adoAction } from '../../test/fixtures';
import { deferred, jsonResponse, mockFetch, renderWithProviders, type Route } from '../../test/utils';
import { ActionCard } from './ActionCard';

const approval: ApprovalOut = {
  id: 'ap-1',
  decision: 'approved',
  decided_by_user_id: 'u-1',
  decided_by_name: 'Avery Approver',
  action_version: 3,
  payload_hash: HASH_V3,
  reason: null,
  created_at: '2026-10-24T10:05:00Z',
  invalidated_at: null,
  invalidated_reason: null,
};

function baseRoutes(detail: () => ActionDetail): Route[] {
  return [
    { method: 'GET', path: '/api/v1/actions/act-1', handler: () => jsonResponse(detail()) },
    { method: 'GET', path: '/api/v1/actions/act-1/history', handler: () => jsonResponse([]) },
    {
      method: 'GET',
      path: '/api/v1/integrations/azure-devops/projects',
      handler: () => jsonResponse({ items: [] }),
    },
  ];
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('<ActionCard> approve flow', () => {
  it('disables while pending, never updates the status optimistically, and sends version + payload_hash', async () => {
    const user = userEvent.setup();
    const pendingApprove = deferred<Response>();
    const { calls } = mockFetch([
      ...baseRoutes(() => adoAction()),
      { method: 'POST', path: '/api/v1/actions/act-1/approve', handler: () => pendingApprove.promise },
    ]);

    renderWithProviders(<ActionCard actionId="act-1" />);

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

    const approveCall = calls.find((c) => c.method === 'POST' && c.url === '/api/v1/actions/act-1/approve');
    expect(approveCall?.body).toEqual({ version: 3, payload_hash: HASH_V3, comment: 'Matches the email' });

    // Server confirms → the confirmed status is shown.
    pendingApprove.resolve(
      jsonResponse(adoAction({ status: 'approved', current_approval: approval, approvals: [approval] })),
    );
    expect(await screen.findByLabelText('Status: Approved')).toBeInTheDocument();
    expect(screen.queryByLabelText('Status: Pending approval')).toBeNull();
    expect(screen.getByTestId('execute-button')).toBeEnabled();
    expect(screen.getByText('Approved by Avery Approver')).toBeInTheDocument();
  });

  it('sends a null comment when none is given', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch([
      ...baseRoutes(() => adoAction()),
      {
        method: 'POST',
        path: '/api/v1/actions/act-1/approve',
        handler: () => jsonResponse(adoAction({ status: 'approved', current_approval: approval })),
      },
    ]);
    renderWithProviders(<ActionCard actionId="act-1" />);
    await user.click(await screen.findByTestId('approve-button'));
    await user.click(await screen.findByTestId('confirm-approve'));
    await screen.findByLabelText('Status: Approved');
    expect(calls.find((c) => c.url.endsWith('/approve'))?.body).toEqual({
      version: 3,
      payload_hash: HASH_V3,
      comment: null,
    });
  });

  it('blocks approval while there are unsaved edits', async () => {
    const user = userEvent.setup();
    mockFetch(baseRoutes(() => adoAction()));
    renderWithProviders(<ActionCard actionId="act-1" />);
    const title = await screen.findByRole('textbox', { name: /^Title/ });
    await user.type(title, ' (edited)');
    expect(screen.getByTestId('approve-button')).toBeDisabled();
    expect(screen.getByText(/Save or discard your edits first/)).toBeInTheDocument();
  });

  it('PATCHes with expected_version and makes the invalidated approval obvious', async () => {
    const user = userEvent.setup();
    const approvedV3 = adoAction({ status: 'approved', current_approval: approval, approvals: [approval] });
    const { calls } = mockFetch([
      ...baseRoutes(() => approvedV3),
      {
        method: 'PATCH',
        path: '/api/v1/actions/act-1',
        handler: (call) => {
          const body = call.body as { payload: Record<string, unknown> };
          return jsonResponse(
            adoAction({
              status: 'pending_approval',
              version: 4,
              payload_hash: HASH_V4,
              payload: body.payload,
              current_approval: null,
              approvals: [
                { ...approval, invalidated_at: '2026-10-24T10:10:00Z', invalidated_reason: 'payload edited' },
              ],
            }),
          );
        },
      },
    ]);
    renderWithProviders(<ActionCard actionId="act-1" />);
    expect(await screen.findByLabelText('Status: Approved')).toBeInTheDocument();

    const title = screen.getByRole('textbox', { name: /^Title/ });
    await user.clear(title);
    await user.type(title, 'Checkout returns HTTP 500 (EU)');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    const patch = await waitFor(() => {
      const call = calls.find((c) => c.method === 'PATCH');
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
    expect(notice).toHaveTextContent(/previous approval by Avery Approver \(v3\) was invalidated/);
    expect(screen.getByLabelText('Status: Pending approval')).toBeInTheDocument();
    expect(screen.getByLabelText('Version 4')).toBeInTheDocument();
    expect(screen.getByTestId('payload-hash')).toHaveTextContent(HASH_V4.slice(0, 12));
    expect(screen.queryByTestId('execute-button')).toBeNull();
  });

  it('shows a "changed — reload" banner on 409 stale_version and reloads on demand', async () => {
    const user = userEvent.setup();
    let detail = adoAction();
    const { calls } = mockFetch([
      ...baseRoutes(() => detail),
      {
        method: 'POST',
        path: '/api/v1/actions/act-1/approve',
        handler: () => {
          detail = adoAction({ version: 5, payload_hash: HASH_V4 });
          return jsonResponse(
            {
              error: {
                code: 'stale_version',
                message: 'The resource was modified. Reload and review the latest version.',
                correlation_id: 'corr-stale',
                details: [],
              },
            },
            409,
          );
        },
      },
    ]);
    renderWithProviders(<ActionCard actionId="act-1" />);
    await user.click(await screen.findByTestId('approve-button'));
    await user.click(await screen.findByTestId('confirm-approve'));

    expect(await screen.findByText('This action changed — reload before continuing')).toBeInTheDocument();
    expect(screen.getByText('ref corr-stale')).toBeInTheDocument();
    expect(screen.getByLabelText('Status: Pending approval')).toBeInTheDocument();

    const getsBefore = calls.filter((c) => c.method === 'GET' && c.url === '/api/v1/actions/act-1').length;
    await user.click(screen.getByRole('button', { name: 'Reload action' }));
    expect(await screen.findByLabelText('Version 5')).toBeInTheDocument();
    expect(calls.filter((c) => c.method === 'GET' && c.url === '/api/v1/actions/act-1').length).toBe(
      getsBefore + 1,
    );
    await waitFor(() =>
      expect(screen.queryByText('This action changed — reload before continuing')).toBeNull(),
    );
  });

  it('execute: queued (no optimistic status), polls, then shows the confirmed result with a mock:// link as text', async () => {
    const user = userEvent.setup();
    const approved = adoAction({ status: 'approved', current_approval: approval, approvals: [approval] });
    const executing = { ...approved, status: 'executing' as const, updated_at: '2026-10-24T10:20:00Z' };
    const completed = {
      ...approved,
      status: 'completed' as const,
      updated_at: '2026-10-24T10:20:05Z',
      external_id: '4711',
      external_url: 'mock://contoso/Payments/_workitems/edit/4711',
      executions: [
        {
          id: 'ex-1',
          attempt_number: 1,
          status: 'succeeded' as const,
          deduplicated: false,
          requested_by_user_id: 'u-1',
          action_version: 3,
          started_at: '2026-10-24T10:20:00Z',
          finished_at: '2026-10-24T10:20:02Z',
          latency_ms: 2100,
          retry_count: 1,
          external_id: '4711',
          external_url: 'mock://contoso/Payments/_workitems/edit/4711',
          provider_status_code: 200,
          error_category: null,
          error_message: null,
        },
      ],
    };
    const sequence: ActionDetail[] = [approved, approved, executing, completed];
    let gets = 0;
    const { calls } = mockFetch([
      {
        method: 'GET',
        path: '/api/v1/actions/act-1',
        handler: () => jsonResponse(sequence[Math.min(gets++, sequence.length - 1)]),
      },
      { method: 'GET', path: '/api/v1/actions/act-1/history', handler: () => jsonResponse([]) },
      {
        method: 'GET',
        path: '/api/v1/integrations/azure-devops/projects',
        handler: () => jsonResponse({ items: [] }),
      },
      {
        method: 'POST',
        path: '/api/v1/actions/act-1/execute',
        // 202: the job is queued; the action is still "approved" until a worker picks it up.
        handler: () => jsonResponse({ action: approved, job_id: 'job-1' }, 202),
      },
    ]);
    renderWithProviders(<ActionCard actionId="act-1" />);
    await user.click(await screen.findByTestId('execute-button'));
    await user.click(await screen.findByTestId('confirm-execute'));

    expect(calls.find((c) => c.url.endsWith('/execute'))?.body).toEqual({
      version: 3,
      payload_hash: HASH_V3,
    });
    expect(await screen.findByText(/Queued — waiting for a worker/)).toBeInTheDocument();
    expect(screen.getByLabelText('Status: Approved')).toBeInTheDocument();
    expect(screen.getByTestId('execute-button')).toBeDisabled();

    expect(await screen.findByLabelText('Status: Executing', {}, { timeout: 8000 })).toBeInTheDocument();
    expect(await screen.findByLabelText('Status: Completed', {}, { timeout: 8000 })).toBeInTheDocument();
    const mockUrl = screen.getAllByText('mock://contoso/Payments/_workitems/edit/4711');
    expect(mockUrl.length).toBeGreaterThan(0);
    for (const node of mockUrl) expect(node.closest('a')).toBeNull();
    expect(screen.getAllByText('Mock').length).toBeGreaterThan(0);
  }, 20_000);

  it('hides controls the API does not permit', async () => {
    mockFetch(
      baseRoutes(() =>
        adoAction({
          permissions: {
            can_edit: false,
            can_approve: false,
            can_reject: false,
            can_cancel: false,
            can_execute: false,
            can_reconcile: false,
          },
        }),
      ),
    );
    renderWithProviders(<ActionCard actionId="act-1" />);
    expect(await screen.findByText('No actions available for you in this state.')).toBeInTheDocument();
    expect(screen.queryByTestId('approve-button')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull();
    expect(screen.getByRole('textbox', { name: /^Title/ })).toBeDisabled();
  });
});
