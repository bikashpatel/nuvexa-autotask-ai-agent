import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/client';
import type { ActionDetail, ActionPermissions, ActionStatus, ApprovalOut } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import {
  ALL_ACTION_PERMISSIONS,
  HASH_V3,
  HASH_V4,
  NO_ACTION_PERMISSIONS,
  actionSummary,
  adoAction,
  approvalFixture,
  followUpAction,
  replyAction,
} from '../../test/fixtures';
import {
  callsTo,
  deferred,
  errorResponse,
  jsonResponse,
  mockFetch,
  renderWithProviders,
  type Route,
} from '../../test/utils';
import { ActionControls, type ActionControlsProps } from './ActionControls';

const BASE = '/api/v1/actions/act-1';
const MUTATING = [
  'Save changes',
  'Discard',
  'Approve',
  'Execute',
  'Retry execution',
  'Reconcile',
  'Reject',
  'Cancel proposal',
];

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderControls(
  action: ActionDetail,
  props: Partial<ActionControlsProps> = {},
  routes: Route[] = [],
) {
  const handlers = {
    onSave: vi.fn(),
    onDiscard: vi.fn(),
    onConflict: vi.fn(),
    onQueued: vi.fn(),
  };
  const fetch = mockFetch(routes);
  const view = renderWithProviders(
    <ActionControls
      action={action}
      dirty={false}
      editable={false}
      saving={false}
      queued={false}
      {...handlers}
      {...props}
    />,
    { withNotifications: true },
  );
  return { ...view, ...handlers, ...fetch };
}

/** Names of the mutating buttons currently rendered, in DOM order. */
function visibleControls(): string[] {
  return screen
    .queryAllByRole('button')
    .map((b) => b.textContent?.trim() ?? '')
    .filter((name) => MUTATING.includes(name));
}

const validApproval = approvalFixture({ action_version: 3, payload_hash: HASH_V3 });

describe('<ActionControls> visibility follows status and API permission flags', () => {
  it.each<[string, Partial<ActionDetail>, string[]]>([
    ['pending_approval', { status: 'pending_approval' }, ['Approve', 'Reject', 'Cancel proposal']],
    [
      'approved',
      { status: 'approved', current_approval: validApproval },
      ['Execute', 'Reject', 'Cancel proposal'],
    ],
    ['failed without an approval', { status: 'failed' }, ['Cancel proposal']],
    [
      'failed with a valid approval',
      { status: 'failed', current_approval: validApproval },
      ['Retry execution', 'Cancel proposal'],
    ],
    [
      'failed with an invalidated approval',
      { status: 'failed', current_approval: { ...validApproval, invalidated_at: '2026-10-24T11:00:00Z' } },
      ['Cancel proposal'],
    ],
    [
      'failed with an approval for another version',
      { status: 'failed', current_approval: { ...validApproval, action_version: 2 } },
      ['Cancel proposal'],
    ],
    [
      'failed with an approval for another payload hash',
      { status: 'failed', current_approval: { ...validApproval, payload_hash: HASH_V4 } },
      ['Cancel proposal'],
    ],
    [
      'failed with a rejection as current decision',
      { status: 'failed', current_approval: { ...validApproval, decision: 'rejected' } },
      ['Cancel proposal'],
    ],
    ['uncertain', { status: 'uncertain' }, ['Reconcile']],
  ])('%s → %j', (_label, overrides, expected) => {
    renderControls(adoAction({ permissions: ALL_ACTION_PERMISSIONS, ...overrides }));
    expect(visibleControls()).toEqual(expected);
  });

  it.each<ActionStatus>(['completed', 'rejected', 'cancelled', 'superseded'])(
    '%s offers no controls',
    (status) => {
      renderControls(
        adoAction({ status, permissions: ALL_ACTION_PERMISSIONS, current_approval: validApproval }),
      );
      expect(visibleControls()).toEqual([]);
      expect(screen.getByText('No actions available for you in this state.')).toBeInTheDocument();
    },
  );

  it('a live execution offers no controls (the server withholds can_reconcile)', () => {
    renderControls(
      adoAction({
        status: 'executing',
        permissions: { ...ALL_ACTION_PERMISSIONS, can_reconcile: false },
        current_approval: validApproval,
      }),
    );
    expect(visibleControls()).toEqual([]);
    expect(screen.getByText('No actions available for you in this state.')).toBeInTheDocument();
  });

  it('an execution whose outcome was never recorded offers Reconcile and explains it', () => {
    // The server sets can_reconcile on an executing action only once its execution lease has expired.
    renderControls(
      adoAction({
        status: 'executing',
        permissions: ALL_ACTION_PERMISSIONS,
        current_approval: validApproval,
      }),
    );
    expect(visibleControls()).toEqual(['Reconcile']);
    expect(screen.getByText(/stopped without recording its outcome/i, { exact: false })).toBeInTheDocument();
  });

  it.each<[keyof ActionPermissions, ActionStatus, ApprovalOut | null, string[]]>([
    ['can_approve', 'pending_approval', null, ['Approve']],
    ['can_approve', 'approved', validApproval, []],
    ['can_reject', 'pending_approval', null, ['Reject']],
    ['can_reject', 'approved', validApproval, ['Reject']],
    ['can_reject', 'failed', null, []],
    ['can_cancel', 'pending_approval', null, ['Cancel proposal']],
    ['can_cancel', 'failed', null, ['Cancel proposal']],
    ['can_cancel', 'uncertain', null, []],
    ['can_execute', 'approved', validApproval, ['Execute']],
    ['can_execute', 'pending_approval', null, []],
    ['can_execute', 'failed', validApproval, ['Retry execution']],
    ['can_reconcile', 'uncertain', null, ['Reconcile']],
    ['can_reconcile', 'executing', null, ['Reconcile']],
    ['can_reconcile', 'failed', null, []],
  ])('only %s on %s → %j', (flag, status, approval, expected) => {
    renderControls(
      adoAction({
        status,
        current_approval: approval,
        permissions: { ...NO_ACTION_PERMISSIONS, [flag]: true },
      }),
    );
    expect(visibleControls()).toEqual(expected);
  });

  it('can_edit (editable) shows a disabled Save when clean', () => {
    renderControls(adoAction({ permissions: { ...NO_ACTION_PERMISSIONS, can_edit: true } }), {
      editable: true,
    });
    expect(visibleControls()).toEqual(['Save changes']);
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
  });

  it('can_edit (editable) + dirty shows Save and Discard wired to the editor', async () => {
    const user = userEvent.setup();
    const { onSave, onDiscard } = renderControls(
      adoAction({ permissions: { ...NO_ACTION_PERMISSIONS, can_edit: true } }),
      { editable: true, dirty: true },
    );
    expect(visibleControls()).toEqual(['Save changes', 'Discard']);
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    await user.click(screen.getByRole('button', { name: 'Discard' }));
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onDiscard).toHaveBeenCalledTimes(1);
  });

  it('no permission flags at all → "No actions available"; permissions: null → "Read-only."', () => {
    const { unmount } = renderControls(adoAction({ permissions: NO_ACTION_PERMISSIONS }));
    expect(screen.getByText('No actions available for you in this state.')).toBeInTheDocument();
    unmount();
    renderControls(adoAction({ permissions: null }));
    expect(screen.getByText('Read-only.')).toBeInTheDocument();
    expect(visibleControls()).toEqual([]);
  });
});

describe('<ActionControls> blocking rules', () => {
  it('unsaved edits block approve and execute, with the reason shown', () => {
    renderControls(adoAction({ permissions: ALL_ACTION_PERMISSIONS }), { dirty: true, editable: true });
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(
      screen.getByText('Save or discard your edits first — approvals bind to the saved payload.'),
    ).toBeInTheDocument();
  });

  it('unsaved edits block execute on an approved action', () => {
    renderControls(
      adoAction({ status: 'approved', current_approval: validApproval, permissions: ALL_ACTION_PERMISSIONS }),
      { dirty: true, editable: true },
    );
    expect(screen.getByTestId('execute-button')).toBeDisabled();
  });

  it('validation errors block approval with an explanation', () => {
    renderControls(adoAction({ validation_errors: ['Project "Ops" is not allowed by tenant policy'] }));
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(screen.getByText('Resolve the validation errors before approving.')).toBeInTheDocument();
    // Reject/cancel stay available.
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled();
  });

  it('while saving every decision is disabled', () => {
    renderControls(adoAction(), { dirty: true, editable: true, saving: true });
    for (const name of ['Approve', 'Reject', 'Cancel proposal', 'Discard']) {
      expect(screen.getByRole('button', { name })).toBeDisabled();
    }
    expect(screen.getByRole('button', { name: 'Save changes' })).toHaveAttribute('data-loading');
  });

  it('a queued job disables re-submission and announces the queue state', () => {
    renderControls(adoAction({ status: 'approved', current_approval: validApproval }), { queued: true });
    expect(screen.getByTestId('execute-button')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Queued — waiting for a worker to pick up the job. Status updates automatically.',
    );
  });

  it('an expired watch (queueStale) no longer promises live updates and leaves the buttons to the permissions', () => {
    renderControls(adoAction({ status: 'approved', current_approval: validApproval }), {
      queueStale: true,
    });
    expect(screen.getByTestId('execute-button')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Still queued — no worker has picked up the job yet and this page has stopped checking. Reload to check the latest status.',
    );
    expect(screen.queryByText(/updates automatically/)).toBeNull();
  });

  it('an expired watch does not grant buttons the permissions withhold', () => {
    renderControls(
      adoAction({
        status: 'approved',
        current_approval: validApproval,
        permissions: { ...ALL_ACTION_PERMISSIONS, can_execute: false, can_reject: false },
      }),
      { queueStale: true },
    );
    expect(screen.queryByTestId('execute-button')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Reject' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Cancel proposal' })).toBeEnabled();
  });
});

describe('<ActionControls> approve dialog', () => {
  it('shows the exact version + payload hash, traps focus, closes on Escape and Back', async () => {
    const user = userEvent.setup();
    renderControls(adoAction({ version: 7, payload_hash: HASH_V4, title: 'Create bug: X' }));
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    const dialog = screen.getByRole('dialog', { name: 'Approve this exact payload?' });
    expect(dialog).toHaveTextContent('You are approving Azure DevOps work item “Create bug: X”');
    expect(within(dialog).getByText('v7')).toBeInTheDocument();
    expect(within(dialog).getByText(HASH_V4.slice(0, 16))).toHaveAttribute('title', HASH_V4);
    expect(within(dialog).getByText('0/1000')).toBeInTheDocument();
    await user.type(within(dialog).getByRole('textbox', { name: 'Comment (optional)' }), 'LGTM');
    expect(within(dialog).getByText('4/1000')).toBeInTheDocument();
    // Regression: the modal's × button had no accessible name (axe button-name, critical).
    expect(within(dialog).getByRole('button', { name: 'Close dialog' })).toBeInTheDocument();
    await expectNoSeriousA11yViolations();

    // Focus stays inside the dialog while tabbing.
    for (let i = 0; i < 6; i += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Approve' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Back' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Approve' }));
    await user.click(screen.getByRole('button', { name: 'Close dialog' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it.each([
    ['Reject', 'Reject this proposal'],
    ['Cancel proposal', 'Cancel this proposal?'],
  ])('the %s dialog has a named close button and passes axe', async (button, title) => {
    const user = userEvent.setup();
    renderControls(adoAction());
    await user.click(screen.getByRole('button', { name: button }));
    const dialog = screen.getByRole('dialog', { name: title });
    expect(within(dialog).getByRole('button', { name: 'Close dialog' })).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
  });

  it('sends exactly the displayed version + payload_hash once (double-click safe) and toasts on success', async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    const action = adoAction({ version: 7, payload_hash: HASH_V4 });
    const { calls } = renderControls(action, {}, [
      { method: 'POST', path: `${BASE}/approve`, handler: () => pending.promise },
    ]);
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByRole('textbox', { name: 'Comment (optional)' }), '  Matches email  ');
    const confirm = within(dialog).getByRole('button', { name: 'Confirm approval' });
    await user.dblClick(confirm);
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(confirm).toHaveAttribute('data-loading');
    expect(within(dialog).getByRole('button', { name: 'Back' })).toBeDisabled();
    // While the request is in flight Escape does not close the dialog.
    await user.keyboard('{Escape}');
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    expect(callsTo(calls, 'POST', `${BASE}/approve`)).toHaveLength(1);
    expect(callsTo(calls, 'POST', `${BASE}/approve`)[0]!.body).toEqual({
      version: 7,
      payload_hash: HASH_V4,
      comment: 'Matches email',
    });

    pending.resolve(jsonResponse(adoAction({ status: 'approved', version: 7, payload_hash: HASH_V4 })));
    expect(await screen.findByText('Version 7 is approved and ready to execute.')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    // The comment was cleared for the next approval.
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    expect(
      within(screen.getByRole('dialog')).getByRole('textbox', { name: 'Comment (optional)' }),
    ).toHaveValue('');
  });

  it('a 409 stale_version is handed to onConflict (no toast); other errors toast with code + reference', async () => {
    const user = userEvent.setup();
    let status = 409;
    const { onConflict } = renderControls(adoAction(), {}, [
      {
        method: 'POST',
        path: `${BASE}/approve`,
        handler: () =>
          status === 409
            ? errorResponse(409, 'stale_version', { correlationId: 'corr-stale' })
            : errorResponse(403, 'permission_denied', {
                message: 'You may not approve your own action.',
                correlationId: 'corr-403',
              }),
      },
    ]);
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    await user.click(screen.getByRole('button', { name: 'Confirm approval' }));
    await waitFor(() => expect(onConflict).toHaveBeenCalledTimes(1));
    const error = onConflict.mock.calls[0]![0] as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.isStaleVersion).toBe(true);
    expect(error.supportReference).toBe('corr-stale');
    expect(screen.queryByText('Approval failed')).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();

    status = 403;
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    await user.click(screen.getByRole('button', { name: 'Confirm approval' }));
    expect(await screen.findByText('Approval failed')).toBeInTheDocument();
    expect(screen.getByText('You may not approve your own action.')).toBeInTheDocument();
    expect(screen.getByText('permission_denied · ref corr-403')).toBeInTheDocument();
    expect(onConflict).toHaveBeenCalledTimes(1);
  });
});

describe('<ActionControls> reject dialog', () => {
  it('requires a reason, trims it, sends one request and toasts', async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    const { calls } = renderControls(adoAction(), {}, [
      { method: 'POST', path: `${BASE}/reject`, handler: () => pending.promise },
    ]);
    await user.click(screen.getByRole('button', { name: 'Reject' }));
    const dialog = screen.getByRole('dialog', { name: 'Reject this proposal' });
    const reason = within(dialog).getByRole('textbox', { name: /Reason/ });
    await waitFor(() => expect(reason).toHaveFocus());
    const confirm = within(dialog).getByRole('button', { name: 'Reject proposal' });
    expect(confirm).toBeDisabled();

    // Blur with an empty reason → inline error.
    await user.type(reason, '   ');
    expect(confirm).toBeDisabled();
    await user.tab();
    expect(reason).toHaveAttribute('aria-invalid', 'true');
    expect(within(dialog).getByText('A reason is required')).toBeInTheDocument();
    expect(calls).toHaveLength(0);

    await user.clear(reason);
    await user.type(reason, '  Duplicate of #12  ');
    expect(within(dialog).queryByText('A reason is required')).toBeNull();
    await user.dblClick(confirm);
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(callsTo(calls, 'POST', `${BASE}/reject`)).toHaveLength(1);
    expect(callsTo(calls, 'POST', `${BASE}/reject`)[0]!.body).toEqual({ reason: 'Duplicate of #12' });

    pending.resolve(jsonResponse(adoAction({ status: 'rejected' })));
    expect(
      await screen.findByText('The proposal was rejected and will not be executed.'),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('refuses a reason longer than 1000 characters without sending', async () => {
    const user = userEvent.setup();
    const { calls } = renderControls(adoAction());
    await user.click(screen.getByRole('button', { name: 'Reject' }));
    const dialog = screen.getByRole('dialog');
    // maxLength stops typing; a programmatic change (e.g. paste via devtools) is still guarded.
    fireEvent.change(within(dialog).getByRole('textbox', { name: /Reason/ }), {
      target: { value: 'r'.repeat(1001) },
    });
    expect(within(dialog).getByText('At most 1000 characters')).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Reject proposal' }));
    expect(calls).toHaveLength(0);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('409 → onConflict; other errors → "Rejection failed" toast', async () => {
    const user = userEvent.setup();
    let code = 'stale_version';
    const { onConflict } = renderControls(adoAction(), {}, [
      {
        method: 'POST',
        path: `${BASE}/reject`,
        handler: () => errorResponse(code === 'stale_version' ? 409 : 500, code),
      },
    ]);
    for (const next of ['stale_version', 'internal_error']) {
      code = next;
      await user.click(screen.getByRole('button', { name: 'Reject' }));
      await user.type(screen.getByRole('textbox', { name: /Reason/ }), 'no');
      await user.click(screen.getByRole('button', { name: 'Reject proposal' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    }
    expect(onConflict).toHaveBeenCalledTimes(1);
    expect(await screen.findByText('Rejection failed')).toBeInTheDocument();
    expect(screen.getByText('internal_error · ref corr-internal_error')).toBeInTheDocument();
  });
});

describe('<ActionControls> cancel dialog', () => {
  it('sends the trimmed optional reason once (null when blank) and toasts', async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    const { calls } = renderControls(adoAction({ status: 'failed' }), {}, [
      { method: 'POST', path: `${BASE}/cancel`, handler: () => pending.promise },
    ]);
    await user.click(screen.getByRole('button', { name: 'Cancel proposal' }));
    const dialog = screen.getByRole('dialog', { name: 'Cancel this proposal?' });
    const confirm = within(dialog).getByRole('button', { name: 'Cancel proposal' });
    await user.dblClick(confirm);
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(callsTo(calls, 'POST', `${BASE}/cancel`)).toHaveLength(1);
    expect(callsTo(calls, 'POST', `${BASE}/cancel`)[0]!.body).toEqual({ reason: null });
    pending.resolve(jsonResponse(adoAction({ status: 'cancelled' })));
    expect(await screen.findByText('The proposal was cancelled.')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('trims a given reason; 409 → onConflict; other errors → "Cancel failed"', async () => {
    const user = userEvent.setup();
    let status = 200;
    const { calls, onConflict } = renderControls(adoAction(), {}, [
      {
        method: 'POST',
        path: `${BASE}/cancel`,
        handler: () =>
          status === 200
            ? jsonResponse(adoAction({ status: 'cancelled' }))
            : errorResponse(status, status === 409 ? 'stale_version' : 'internal_error'),
      },
    ]);
    const open = async () => {
      await user.click(screen.getAllByRole('button', { name: 'Cancel proposal' })[0]!);
      return screen.getByRole('dialog');
    };
    let dialog = await open();
    await user.type(within(dialog).getByRole('textbox', { name: 'Reason (optional)' }), '  not needed  ');
    await user.click(within(dialog).getByRole('button', { name: 'Cancel proposal' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(callsTo(calls, 'POST', `${BASE}/cancel`)[0]!.body).toEqual({ reason: 'not needed' });

    status = 409;
    dialog = await open();
    // The reason was reset after the successful cancel.
    expect(within(dialog).getByRole('textbox', { name: 'Reason (optional)' })).toHaveValue('');
    await user.click(within(dialog).getByRole('button', { name: 'Cancel proposal' }));
    await waitFor(() => expect(onConflict).toHaveBeenCalledTimes(1));

    status = 500;
    dialog = await open();
    await user.click(within(dialog).getByRole('button', { name: 'Cancel proposal' }));
    expect(await screen.findByText('Cancel failed')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: 'Cancel proposal' })[0]!);
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Back' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('<ActionControls> execute dialog', () => {
  it.each<[string, ActionDetail, RegExp | null]>([
    [
      'azure_devops',
      adoAction({ status: 'approved', current_approval: validApproval }),
      /creates a work item in Azure DevOps/,
    ],
    [
      'reply_drafts',
      replyAction({ status: 'approved', current_approval: validApproval }),
      /stores a reply draft in NUVEXA\. Drafts are never sent by NUVEXA/,
    ],
    ['internal_tasks', followUpAction({ status: 'approved', current_approval: validApproval }), null],
  ])('explains the effect for %s targets', async (_target, action, copy) => {
    const user = userEvent.setup();
    renderControls(action);
    await user.click(screen.getByTestId('execute-button'));
    const dialog = screen.getByRole('dialog', { name: 'Execute approved action?' });
    if (copy) expect(dialog).toHaveTextContent(copy);
    else expect(within(dialog).queryByRole('alert')).toBeNull();
    expect(within(dialog).getByRole('button', { name: 'Close dialog' })).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
    expect(within(dialog).getByRole('button', { name: 'Execute now' })).toBeInTheDocument();
  });

  it('sends exactly the displayed version + hash once, then reports the queued job', async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    const action = adoAction({
      status: 'approved',
      version: 7,
      payload_hash: HASH_V4,
      current_approval: approvalFixture({ action_version: 7, payload_hash: HASH_V4 }),
    });
    const { calls, onQueued } = renderControls(action, {}, [
      { method: 'POST', path: `${BASE}/execute`, handler: () => pending.promise },
    ]);
    await user.click(screen.getByTestId('execute-button'));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('v7')).toBeInTheDocument();
    const confirm = within(dialog).getByTestId('confirm-execute');
    await user.dblClick(confirm);
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(screen.getByTestId('execute-button')).toBeDisabled();
    expect(callsTo(calls, 'POST', `${BASE}/execute`)).toHaveLength(1);
    expect(callsTo(calls, 'POST', `${BASE}/execute`)[0]!.body).toEqual({ version: 7, payload_hash: HASH_V4 });

    pending.resolve(
      jsonResponse(
        { action: actionSummary({ status: 'approved', updated_at: '2026-10-24T10:20:00Z' }), job_id: 'j1' },
        202,
      ),
    );
    await waitFor(() => expect(onQueued).toHaveBeenCalledWith('2026-10-24T10:20:00Z'));
    expect(await screen.findByText('Execution queued')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('failed + valid approval → "Retry execution" dialog; errors toast or hand 409 to onConflict', async () => {
    const user = userEvent.setup();
    let status = 409;
    const { onConflict } = renderControls(
      adoAction({ status: 'failed', current_approval: validApproval }),
      {},
      [
        {
          method: 'POST',
          path: `${BASE}/execute`,
          handler: () => errorResponse(status, status === 409 ? 'stale_version' : 'integration_unavailable'),
        },
      ],
    );
    await user.click(screen.getByRole('button', { name: 'Retry execution' }));
    expect(screen.getByRole('dialog', { name: 'Retry execution?' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry now' }));
    await waitFor(() => expect(onConflict).toHaveBeenCalledTimes(1));

    status = 503;
    await user.click(screen.getByRole('button', { name: 'Retry execution' }));
    await user.click(screen.getByRole('button', { name: 'Retry now' }));
    expect(await screen.findByText('Execution failed to start')).toBeInTheDocument();
    expect(
      screen.getByText('integration_unavailable · ref corr-integration_unavailable'),
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Retry execution' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Back' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('<ActionControls> reconcile', () => {
  it('POSTs reconcile (no body) once while pending and reports the queued job', async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    const { calls, onQueued } = renderControls(
      adoAction({ status: 'uncertain', permissions: ALL_ACTION_PERMISSIONS }),
      {},
      [{ method: 'POST', path: `${BASE}/reconcile`, handler: () => pending.promise }],
    );
    const button = screen.getByRole('button', { name: 'Reconcile' });
    await user.dblClick(button);
    await waitFor(() => expect(button).toBeDisabled());
    expect(callsTo(calls, 'POST', `${BASE}/reconcile`)).toHaveLength(1);
    expect(callsTo(calls, 'POST', `${BASE}/reconcile`)[0]!.body).toBeUndefined();
    pending.resolve(
      jsonResponse(
        { action: actionSummary({ status: 'uncertain', updated_at: '2026-10-24T11:00:00Z' }), job_id: 'j' },
        202,
      ),
    );
    await waitFor(() => expect(onQueued).toHaveBeenCalledWith('2026-10-24T11:00:00Z'));
    expect(await screen.findByText('Reconciliation queued')).toBeInTheDocument();
  });

  it('409 → onConflict; other errors → "Reconcile failed" toast', async () => {
    const user = userEvent.setup();
    let status = 409;
    const { onConflict } = renderControls(
      adoAction({ status: 'uncertain', permissions: ALL_ACTION_PERMISSIONS }),
      {},
      [
        {
          method: 'POST',
          path: `${BASE}/reconcile`,
          handler: () => errorResponse(status, status === 409 ? 'stale_version' : 'timeout'),
        },
      ],
    );
    await user.click(screen.getByRole('button', { name: 'Reconcile' }));
    await waitFor(() => expect(onConflict).toHaveBeenCalledTimes(1));
    status = 504;
    await user.click(screen.getByRole('button', { name: 'Reconcile' }));
    expect(await screen.findByText('Reconcile failed')).toBeInTheDocument();
  });
});

describe('regression: two clicks before the next render send ONE write', () => {
  // `disabled`/`loading` only apply after React re-renders; a synchronous guard must stop the
  // second click (previously both clicks were sent).
  it.each<[string, Partial<ActionDetail>, string, string | null, string]>([
    ['approve', {}, 'Approve', 'Confirm approval', 'approve'],
    ['reject', {}, 'Reject', 'Reject proposal', 'reject'],
    ['cancel', {}, 'Cancel proposal', 'Cancel proposal', 'cancel'],
    ['execute', { status: 'approved', current_approval: validApproval }, 'Execute', 'Execute now', 'execute'],
    [
      'reconcile',
      { status: 'uncertain', permissions: ALL_ACTION_PERMISSIONS },
      'Reconcile',
      null,
      'reconcile',
    ],
  ])('%s', async (_name, overrides, trigger, confirmName, endpoint) => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    const { calls } = renderControls(adoAction(overrides), {}, [
      { method: 'POST', path: `${BASE}/${endpoint}`, handler: () => pending.promise },
    ]);
    let target = screen.getByRole('button', { name: trigger });
    if (confirmName) {
      await user.click(target);
      const dialog = screen.getByRole('dialog');
      if (endpoint === 'reject')
        await user.type(within(dialog).getByRole('textbox', { name: /Reason/ }), 'dup');
      target = within(dialog).getByRole('button', { name: confirmName });
    }
    fireEvent.click(target);
    fireEvent.click(target);
    await waitFor(() => expect(target).toBeDisabled());
    expect(callsTo(calls, 'POST', `${BASE}/${endpoint}`)).toHaveLength(1);

    // After the server answers, the guard is released for the next deliberate write.
    pending.resolve(errorResponse(500, 'internal_error'));
    expect(await screen.findByText('internal_error · ref corr-internal_error')).toBeInTheDocument();
  });

  it('releases the guard after a failed write so the reviewer can try again', async () => {
    const user = userEvent.setup();
    const { calls } = renderControls(adoAction(), {}, [
      { method: 'POST', path: `${BASE}/approve`, handler: () => errorResponse(503, 'unavailable') },
    ]);
    for (let i = 0; i < 2; i += 1) {
      await user.click(screen.getByRole('button', { name: 'Approve' }));
      await user.click(screen.getByRole('button', { name: 'Confirm approval' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    }
    expect(callsTo(calls, 'POST', `${BASE}/approve`)).toHaveLength(2);
  });
});

describe('dialogs cannot be dismissed while their write is in flight', () => {
  it.each<[string, Partial<ActionDetail>, string, string, string]>([
    ['reject', {}, 'Reject', 'Reject proposal', 'reject'],
    ['cancel', {}, 'Cancel proposal', 'Cancel proposal', 'cancel'],
    ['execute', { status: 'approved', current_approval: validApproval }, 'Execute', 'Execute now', 'execute'],
  ])('%s', async (_name, overrides, trigger, confirmName, endpoint) => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    renderControls(adoAction(overrides), {}, [
      { method: 'POST', path: `${BASE}/${endpoint}`, handler: () => pending.promise },
    ]);
    await user.click(screen.getByRole('button', { name: trigger }));
    const dialog = screen.getByRole('dialog');
    if (endpoint === 'reject')
      await user.type(within(dialog).getByRole('textbox', { name: /Reason/ }), 'dup');
    const confirm = within(dialog).getByRole('button', { name: confirmName });
    await user.click(confirm);
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(within(dialog).getByRole('button', { name: 'Back' })).toBeDisabled();
    await user.keyboard('{Escape}');
    await user.click(within(dialog).getByRole('button', { name: 'Close dialog' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    pending.resolve(jsonResponse(adoAction()));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});
