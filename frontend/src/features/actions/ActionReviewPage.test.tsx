import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActionDetail, EmailDetail, MeOut, WorkflowDetail } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import {
  NO_ACTION_PERMISSIONS,
  PERSONAS,
  XSS_IMG,
  XSS_TEXT,
  actionSummary,
  adoAction,
  analysis,
  emailDetail,
  followUpAction,
  replyAction,
  workflowDetail,
  workflowRun,
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
import { ActionReviewPage } from './ActionReviewPage';

const WORKFLOW = '/api/v1/workflows/run-1';
const EMAIL = '/api/v1/emails/email-1';

interface Setup {
  action?: () => ActionDetail;
  workflow?: () => WorkflowDetail | Response | Promise<Response>;
  email?: () => EmailDetail | Response | Promise<Response>;
  extra?: Route[];
  session?: MeOut;
}

function toResponse(value: unknown): Response | Promise<Response> {
  return value instanceof Response || value instanceof Promise ? value : jsonResponse(value);
}

function renderPage({
  action = () => adoAction(),
  workflow = () => workflowDetail(),
  email = () => emailDetail(),
  extra = [],
  session,
}: Setup = {}) {
  const fetch = mockFetch([
    ...extra,
    { method: 'GET', path: '/api/v1/actions/act-1', handler: () => jsonResponse(action()) },
    { method: 'GET', path: /^\/api\/v1\/actions\/[^/]+\/history$/, handler: () => jsonResponse([]) },
    { method: 'GET', path: WORKFLOW, handler: () => toResponse(workflow()) },
    { method: 'GET', path: EMAIL, handler: () => toResponse(email()) },
    {
      method: 'GET',
      path: '/api/v1/integrations/azure-devops/projects',
      handler: () => jsonResponse({ items: [] }),
    },
  ]);
  const view = renderWithProviders(<ActionReviewPage />, {
    path: '/actions/:actionId',
    route: '/actions/act-1',
    withNotifications: true,
    session,
    extraRoutes: [
      { path: '/approvals', element: <h1>Approvals queue</h1> },
      { path: '/workflows/:id', element: <h1>Workflow page</h1> },
      { path: '/emails/:id', element: <h1>Email page</h1> },
    ],
  });
  return { ...view, ...fetch };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('<ActionReviewPage> states', () => {
  it('shows a skeleton while the action loads', async () => {
    const pending = deferred<Response>();
    mockFetch([{ method: 'GET', path: '/api/v1/actions/act-1', handler: () => pending.promise }]);
    renderWithProviders(<ActionReviewPage />, { path: '/actions/:actionId', route: '/actions/act-1' });
    expect(screen.getByLabelText('Loading')).toHaveAttribute('aria-busy', 'true');
    pending.resolve(errorResponse(404, 'not_found'));
    expect(await screen.findByText('Not found')).toBeInTheDocument();
  });

  it('shows the error with correlation id and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = renderPage({
      extra: [
        {
          method: 'GET',
          path: '/api/v1/actions/act-1',
          handler: () =>
            fail
              ? errorResponse(500, 'internal_error', { message: 'Boom', correlationId: 'corr-page' })
              : jsonResponse(adoAction()),
        },
      ],
    });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Boom');
    expect(alert).toHaveTextContent('corr-page');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(
      await screen.findByRole('heading', { level: 1, name: 'URGENT: checkout broken' }),
    ).toBeInTheDocument();
    expect(callsTo(calls, 'GET', '/api/v1/actions/act-1')).toHaveLength(2);
  });
});

describe('<ActionReviewPage> content', () => {
  it('renders breadcrumbs, header, the original email, the analysis and the action card', async () => {
    renderPage({
      workflow: () =>
        workflowDetail({
          analysis: analysis({
            injection_flags: [
              {
                rule: 'ignore_previous_instructions',
                severity: 'high',
                excerpt: 'IGNORE PREVIOUS INSTRUCTIONS',
              },
            ],
          }),
        }),
    });
    expect(
      await screen.findByRole('heading', { level: 1, name: 'URGENT: checkout broken' }),
    ).toBeInTheDocument();
    expect(screen.getByText('Action review · Azure DevOps work item')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Approvals' })).toHaveAttribute('href', '/approvals');
    expect(await screen.findByText('Analysis attempt #1')).toBeInTheDocument();
    expect(screen.getAllByLabelText('Status: Pending approval').length).toBeGreaterThanOrEqual(1);
    expect(await screen.findByTestId('injection-warning')).toHaveTextContent('IGNORE PREVIOUS INSTRUCTIONS');
    expect(screen.getByRole('region', { name: 'AI analysis' })).toBeInTheDocument();

    const original = screen.getByRole('region', { name: 'Original email' });
    expect(
      await within(original).findByText(/Checkout returns HTTP 500 for EU customers since/),
    ).toBeInTheDocument();
    expect(within(original).getByRole('link', { name: 'Open email' })).toHaveAttribute(
      'href',
      '/emails/email-1',
    );
    expect(screen.getByRole('heading', { name: 'Proposed action' })).toBeInTheDocument();
    expect(
      await screen.findByRole('article', { name: 'Create bug: Checkout fails with 500 for EU customers' }),
    ).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
  });

  it('falls back to the action title when there is no email subject', async () => {
    renderPage({ action: () => adoAction({ email_subject: null }) });
    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: 'Create bug: Checkout fails with 500 for EU customers',
      }),
    ).toBeInTheDocument();
  });

  it('navigates via the breadcrumb, the workflow timeline button and the email link', async () => {
    const user = userEvent.setup();
    const { router, unmount } = renderPage();
    await user.click(await screen.findByRole('link', { name: 'Workflow timeline' }));
    expect(router.state.location.pathname).toBe('/workflows/run-1');
    expect(screen.getByRole('heading', { name: 'Workflow page' })).toBeInTheDocument();
    unmount();
    vi.unstubAllGlobals();

    const second = renderPage();
    await user.click(await screen.findByRole('link', { name: 'Approvals' }));
    expect(second.router.state.location.pathname).toBe('/approvals');
  });

  it('renders the email body and subject as plain text (no HTML elements are created)', async () => {
    const { container } = renderPage({
      email: () => emailDetail({ subject: XSS_IMG, body_text: XSS_TEXT }),
    });
    const original = await screen.findByRole('region', { name: 'Original email' });
    expect(await within(original).findByText(XSS_TEXT)).toBeInTheDocument();
    expect(within(original).getByText(XSS_IMG)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });

  it('original email: loading, "(no subject)", content-changed warning, error + retry', async () => {
    const user = userEvent.setup();
    const pendingEmail = deferred<Response>();
    let emailCall = 0;
    renderPage({
      email: () => {
        emailCall += 1;
        if (emailCall === 1) return pendingEmail.promise;
        return emailDetail({ subject: '', content_changed_since_analysis: true });
      },
    });
    const original = await screen.findByRole('region', { name: 'Original email' });
    expect(within(original).getByLabelText('Loading email')).toHaveAttribute('aria-busy', 'true');
    pendingEmail.resolve(errorResponse(503, 'mailbox_unavailable', { correlationId: 'corr-mail' }));
    expect(await within(original).findByText('corr-mail')).toBeInTheDocument();
    await user.click(within(original).getByRole('button', { name: 'Retry' }));
    expect(await within(original).findByText('(no subject)')).toBeInTheDocument();
    expect(
      within(original).getByText('Content changed since analysis — consider requesting re-analysis.'),
    ).toBeInTheDocument();
  });

  it('analysis: skeleton while loading, "no analysis" alert, and an error with retry', async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    let call = 0;
    renderPage({
      workflow: () => {
        call += 1;
        if (call === 1) return pending.promise;
        if (call === 2) return errorResponse(500, 'internal_error', { correlationId: 'corr-wf' });
        return workflowDetail({ analysis: null });
      },
    });
    await screen.findByRole('heading', { level: 1 });
    expect(screen.queryByRole('region', { name: 'AI analysis' })).toBeNull();
    pending.resolve(errorResponse(500, 'internal_error', { correlationId: 'corr-wf' }));
    expect(await screen.findByText('corr-wf')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: 'Retry' })[0]!);
    // Retry #1 fails again, retry #2 succeeds without an analysis.
    await waitFor(() => expect(call).toBeGreaterThanOrEqual(2));
    const retry = await screen.findAllByRole('button', { name: 'Retry' });
    await user.click(retry[0]!);
    expect(await screen.findByText('No analysis is available for this run yet.')).toBeInTheDocument();
  });

  it('a run without id shows no workflow link, no re-analysis and no analysis', async () => {
    renderPage({ action: () => adoAction({ workflow_run_id: '' }) });
    expect(await screen.findByText('No analysis is available for this run yet.')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Workflow timeline' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Request re-analysis' })).toBeNull();
  });
});

describe('<ActionReviewPage> sibling proposals', () => {
  const siblings = () =>
    workflowDetail({
      actions: [
        actionSummary(),
        actionSummary({
          id: 'act-2',
          title: 'Reply to Dana: acknowledge the outage',
          action_type: 'propose_email_reply',
        }),
        actionSummary({ id: 'act-3', title: 'Old follow-up', status: 'superseded' }),
      ],
    });
  const siblingRoutes: Route[] = [
    {
      method: 'GET',
      path: '/api/v1/actions/act-2',
      handler: () => jsonResponse(replyAction({ id: 'act-2' })),
    },
    {
      method: 'GET',
      path: '/api/v1/actions/act-3',
      handler: () =>
        jsonResponse(followUpAction({ id: 'act-3', title: 'Old follow-up', status: 'superseded' })),
    },
  ];

  it('shows the focused proposal plus open siblings; superseded ones only on demand', async () => {
    const user = userEvent.setup();
    renderPage({ workflow: siblings, extra: siblingRoutes });
    expect(await screen.findByRole('heading', { name: 'Proposed actions' })).toBeInTheDocument();
    expect(
      await screen.findByRole('article', { name: 'Reply to Dana: acknowledge the outage' }),
    ).toBeInTheDocument();
    const focused = screen.getByTestId('action-card-act-1');
    expect(focused).toHaveAttribute('style', expect.stringContaining('border-width: 2px'));
    expect(screen.queryByRole('article', { name: 'Old follow-up' })).toBeNull();

    const toggle = screen.getByRole('switch', { name: 'Show superseded (1)' });
    await user.click(toggle);
    expect(await screen.findByRole('article', { name: 'Old follow-up' })).toBeInTheDocument();
    await user.click(toggle);
    expect(screen.queryByRole('article', { name: 'Old follow-up' })).toBeNull();
  });

  it('a single proposal has no superseded toggle and no focus ring', async () => {
    renderPage();
    const card = await screen.findByTestId('action-card-act-1');
    expect(card).not.toHaveAttribute('style', expect.stringContaining('border-width: 2px'));
    expect(screen.queryByRole('switch')).toBeNull();
  });
});

describe('<ActionReviewPage> re-analysis', () => {
  it('asks for confirmation, POSTs reanalyze and closes on success', async () => {
    const user = userEvent.setup();
    const { calls } = renderPage({
      extra: [
        {
          method: 'POST',
          path: `${WORKFLOW}/reanalyze`,
          handler: () => jsonResponse({ job_ids: ['job-9'], message: 'Re-analysis queued for run-1.' }, 202),
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: 'Request re-analysis' }));
    const dialog = screen.getByRole('dialog', { name: 'Request re-analysis?' });
    expect(dialog).toHaveTextContent(
      /Every open proposal of this run — including approved but not yet executed ones/,
    );
    await user.click(within(dialog).getByRole('button', { name: 'Re-analyze' }));
    expect(await screen.findByText('Re-analysis queued for run-1.')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(callsTo(calls, 'POST', `${WORKFLOW}/reanalyze`)).toHaveLength(1);
  });

  it('can be dismissed without a request', async () => {
    const user = userEvent.setup();
    const { calls } = renderPage();
    await user.click(await screen.findByRole('button', { name: 'Request re-analysis' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(callsTo(calls, 'POST', `${WORKFLOW}/reanalyze`)).toHaveLength(0);
  });

  it.each([
    ['completed (terminal)', 'completed'],
    ['cancelled (terminal)', 'cancelled'],
    ['in flight', 'classified'],
  ] as const)('is not offered when the run is %s', async (_label, status) => {
    renderPage({ workflow: () => workflowDetail({ run: workflowRun({ status }) }) });
    // The run header only renders once the workflow has loaded.
    expect(await screen.findByText('Analysis attempt #1')).toBeInTheDocument();
    expect(document.querySelector(`[data-status="${status}"]`)).not.toBeNull();
    await screen.findByRole('article', { name: adoAction().title });
    expect(screen.queryByRole('button', { name: 'Request re-analysis' })).toBeNull();
  });
});

describe('<ActionReviewPage> in-flight runs (polling with fake timers)', () => {
  it.each([
    [
      workflowRun({ status: 'classified', current_step: 'classify' }),
      'The workflow is running (classify). This page refreshes automatically.',
    ],
    [
      workflowRun({ status: 'extracted', current_step: null }),
      'The workflow is running (extracted). This page refreshes automatically.',
    ],
    [
      workflowRun({ status: 'executing', current_step: 'execute' }),
      'The workflow is executing an approved action. This page refreshes automatically.',
    ],
  ])('explains the in-flight state (%#)', async (run, text) => {
    renderPage({ workflow: () => workflowDetail({ run }) });
    expect(await screen.findByRole('status', { name: '' })).toHaveTextContent(text);
  });

  it('polls the workflow every 2 s while it is in flight and stops once it waits for approval', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let detail = workflowDetail({ run: workflowRun({ status: 'validated', current_step: 'validate' }) });
    const { calls } = renderPage({ workflow: () => detail });
    expect(await screen.findByText(/The workflow is running \(validate\)/)).toBeInTheDocument();
    const first = callsTo(calls, 'GET', WORKFLOW).length;
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(callsTo(calls, 'GET', WORKFLOW).length).toBe(first + 1);

    detail = workflowDetail();
    await act(() => vi.advanceTimersByTimeAsync(2000));
    await waitFor(() => expect(screen.queryByText(/The workflow is running/)).toBeNull());
    const settled = callsTo(calls, 'GET', WORKFLOW).length;
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(callsTo(calls, 'GET', WORKFLOW)).toHaveLength(settled);
  });

  it('keeps polling while any action of the run is executing', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let detail = workflowDetail({
      run: workflowRun({ status: 'approved' }),
      actions: [actionSummary({ status: 'executing' })],
    });
    const { calls } = renderPage({ workflow: () => detail });
    await screen.findByRole('article', { name: adoAction().title });
    await waitFor(() => expect(callsTo(calls, 'GET', WORKFLOW).length).toBeGreaterThan(0));
    const first = callsTo(calls, 'GET', WORKFLOW).length;
    await act(() => vi.advanceTimersByTimeAsync(4000));
    expect(callsTo(calls, 'GET', WORKFLOW).length).toBeGreaterThanOrEqual(first + 2);

    detail = workflowDetail({
      run: workflowRun({ status: 'completed' }),
      actions: [actionSummary({ status: 'completed' })],
    });
    await act(() => vi.advanceTimersByTimeAsync(2000));
    const settled = callsTo(calls, 'GET', WORKFLOW).length;
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(callsTo(calls, 'GET', WORKFLOW)).toHaveLength(settled);
  });
});

describe('<ActionReviewPage> viewer persona', () => {
  it('sees no mutating controls at all and every payload input is disabled', async () => {
    renderPage({
      session: PERSONAS.viewer,
      action: () => adoAction({ permissions: NO_ACTION_PERMISSIONS }),
    });
    const card = await screen.findByRole('article', { name: adoAction().title });
    expect(await within(card).findByText('No actions available for you in this state.')).toBeInTheDocument();
    for (const name of [
      'Approve',
      'Reject',
      'Execute',
      'Retry execution',
      'Cancel proposal',
      'Save changes',
      'Discard',
      'Reconcile',
      'Request re-analysis',
      'Validate with Azure DevOps',
      'Add field',
    ]) {
      const button = screen.queryByRole('button', { name });
      if (button) expect(button).toBeDisabled();
      else expect(button).toBeNull();
    }
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Request re-analysis' })).toBeNull();
    for (const input of within(card).getAllByRole('textbox')) expect(input).toBeDisabled();
    for (const input of within(card).getAllByRole('combobox')) expect(input).toBeDisabled();
    expect(within(card).getByText('Payload (read-only)')).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
  });
});
