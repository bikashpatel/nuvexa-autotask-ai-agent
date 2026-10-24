import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { EmailDetail, WorkflowDetail } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import {
  PERSONAS,
  XSS_TEXT,
  actionSummary,
  analysis,
  emailDetail,
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
  type RenderOptions,
  type Route,
} from '../../test/utils';
import { EmailDetailPage } from './EmailDetailPage';

const EMAIL = '/api/v1/emails/email-1';
const RUN = '/api/v1/workflows/run-1';

type Reply = Response | Promise<Response>;

function setup({
  email = () => jsonResponse(emailDetail()),
  workflow = () => jsonResponse(workflowDetail()),
  extra = [],
  ...options
}: {
  email?: () => Reply;
  workflow?: () => Reply;
  extra?: Route[];
} & RenderOptions = {}) {
  const mock = mockFetch([
    { method: 'GET', path: EMAIL, handler: email },
    { method: 'GET', path: RUN, handler: workflow },
    ...extra,
  ]);
  const rendered = renderWithProviders(<EmailDetailPage />, {
    path: '/emails/:emailId',
    route: '/emails/email-1',
    ...options,
  });
  return { ...mock, ...rendered };
}

function detailWith(run: Partial<WorkflowDetail['run']>, rest: Partial<WorkflowDetail> = {}) {
  return jsonResponse(workflowDetail({ run: workflowRun(run), ...rest }));
}

const json = (body: EmailDetail) => () => jsonResponse(body);

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('<EmailDetailPage> email states', () => {
  it('shows a busy skeleton while the email loads', () => {
    const hold = deferred<Response>();
    setup({ email: () => hold.promise });
    expect(screen.getByLabelText('Loading')).toHaveAttribute('aria-busy', 'true');
  });

  it('shows "Not found" for a 404 and retries on demand', async () => {
    const user = userEvent.setup();
    let missing = true;
    const { calls } = setup({
      email: () =>
        missing
          ? errorResponse(404, 'not_found', { correlationId: 'corr-404' })
          : jsonResponse(emailDetail()),
    });
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Not found')).toBeInTheDocument();
    expect(alert).toHaveTextContent('It may have been removed, or it is not visible to your account.');
    expect(within(alert).getByText('corr-404')).toBeInTheDocument();
    missing = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(
      await screen.findByRole('heading', { level: 1, name: 'URGENT: checkout broken' }),
    ).toBeInTheDocument();
    expect(callsTo(calls, 'GET', EMAIL)).toHaveLength(2);
  });
});

describe('<EmailDetailPage> message', () => {
  it('renders headers, badges, the plain-text body, attachments and the changed warning', async () => {
    const { container } = setup({
      email: json(
        emailDetail({
          body_text: `Checkout is down.\n${XSS_TEXT}`,
          has_attachments: true,
          content_changed_since_analysis: true,
          attachments: [
            { name: 'trace.log', content_type: 'text/plain', size: 2048, is_inline: false },
            { name: 'logo.png', content_type: null, size: null, is_inline: true },
          ],
        }),
      ),
    });
    expect(
      await screen.findByRole('heading', { level: 1, name: 'URGENT: checkout broken' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Inbox' })).toHaveAttribute('href', '/inbox');
    expect(screen.getByLabelText('Status: Awaiting review')).toBeInTheDocument();
    expect(screen.getByLabelText('Priority: High')).toBeInTheDocument();
    expect(screen.getByText('2 attachments')).toBeInTheDocument();
    expect(screen.getByText('Content changed since analysis')).toBeInTheDocument();

    // Untrusted body: visible as text, nothing is parsed into elements.
    expect(screen.getByText(/Checkout is down\./)).toHaveTextContent(XSS_TEXT);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();

    const table = screen.getByRole('table', { name: 'Attachments' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(
      within(rows[0]!)
        .getAllByRole('cell')
        .map((c) => c.textContent),
    ).toEqual(['trace.log', 'text/plain', '2.0 KB', 'No']);
    expect(
      within(rows[1]!)
        .getAllByRole('cell')
        .map((c) => c.textContent),
    ).toEqual(['logo.png', '—', '—', 'Yes']);

    const outlook = screen.getByRole('link', { name: /Outlook on the web/ });
    expect(outlook).toHaveAttribute('rel', 'noopener noreferrer');
    expect(outlook).toHaveAttribute('target', '_blank');

    await screen.findByText('Latest analysis');
    await expectNoSeriousA11yViolations(container);
  });

  it('uses "(no subject)", a singular attachment badge and omits the attachments table when empty', async () => {
    setup({
      email: json(
        emailDetail({
          subject: '',
          has_attachments: true,
          attachments: [{ name: 'a.txt', content_type: 'text/plain', size: 10, is_inline: false }],
        }),
      ),
    });
    expect(await screen.findByRole('heading', { level: 1, name: '(no subject)' })).toBeInTheDocument();
    expect(screen.getByText('1 attachment')).toBeInTheDocument();
    expect(screen.queryByText('Content changed since analysis')).toBeNull();
  });

  it('has no attachments section without attachments', async () => {
    setup({ email: json(emailDetail({ has_attachments: false, attachments: [] })) });
    await screen.findByRole('heading', { level: 1 });
    expect(screen.queryByRole('table', { name: 'Attachments' })).toBeNull();
    expect(screen.queryByText(/attachment/)).toBeNull();
  });
});

describe('<EmailDetailPage> latest workflow panel', () => {
  it('offers Analyze when the email was never analyzed (workflow:run only)', async () => {
    const user = userEvent.setup();
    const { calls } = setup({
      session: PERSONAS.member,
      withNotifications: true,
      email: json(emailDetail({ latest_workflow_run_id: null, analysis_status: 'not_analyzed' })),
      extra: [
        {
          method: 'POST',
          path: '/api/v1/emails/email-1/analyze',
          handler: () => jsonResponse({ workflow_run: workflowRun(), created: true, job_id: 'j' }, 202),
        },
      ],
    });
    expect(await screen.findByText('This email has not been analyzed yet.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Analyze' }));
    expect(await screen.findByText('Analysis started')).toBeInTheDocument();
    expect(callsTo(calls, 'POST', '/api/v1/emails/email-1/analyze')).toHaveLength(1);
    expect(callsTo(calls, 'GET', RUN)).toHaveLength(0);
  });

  it('shows no Analyze button to a viewer', async () => {
    setup({
      session: PERSONAS.viewer,
      email: json(emailDetail({ latest_workflow_run_id: null })),
    });
    expect(await screen.findByText('This email has not been analyzed yet.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Analyze' })).toBeNull();
  });

  it('shows a skeleton while the run loads and an error with retry when it fails', async () => {
    const user = userEvent.setup();
    const hold = deferred<Response>();
    let reply: () => Reply = () => hold.promise;
    const { calls } = setup({ workflow: () => reply() });
    await screen.findByRole('heading', { level: 1 });
    expect(screen.getByLabelText('Loading')).toHaveAttribute('aria-busy', 'true');
    hold.resolve(errorResponse(500, 'internal_error', { correlationId: 'corr-run' }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('corr-run')).toBeInTheDocument();
    reply = () => jsonResponse(workflowDetail());
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Latest analysis')).toBeInTheDocument();
    expect(callsTo(calls, 'GET', RUN)).toHaveLength(2);
  });

  it('summarises the run, analysis, injection flags and open proposals', async () => {
    const user = userEvent.setup();
    const { router } = setup({
      workflow: () =>
        jsonResponse(
          workflowDetail({
            run: workflowRun({ error_message: 'Extraction retried once' }),
            analysis: analysis({
              injection_flags: [
                { rule: 'ignore_previous_instructions', severity: 'high', excerpt: 'IGNORE' },
              ],
            }),
            actions: [
              actionSummary({ id: 'act-old', status: 'superseded', title: 'Old proposal' }),
              actionSummary(),
            ],
          }),
        ),
      extraRoutes: [{ path: '/actions/:actionId', element: <p>Action review</p> }],
    });
    const panel = (await screen.findByText('Latest analysis')).closest('section')!;
    const scope = within(panel);
    // Run status badge + the open proposal's status badge.
    expect(scope.getAllByLabelText('Status: Pending approval')).toHaveLength(2);
    expect(scope.getByText('#1')).toBeInTheDocument();
    expect(scope.getByText('Await review')).toBeInTheDocument();
    expect(scope.getByText('heuristic heuristic-demo')).toBeInTheDocument();
    expect(scope.getByText('Extraction retried once')).toBeInTheDocument();
    expect(scope.getByText('Prompt-injection indicators detected (1)')).toBeInTheDocument();
    expect(scope.getByLabelText(/Medium confidence/)).toBeInTheDocument();
    expect(scope.queryByText(/Old proposal/)).toBeNull();
    const proposal = scope.getByRole('link', {
      name: 'Azure DevOps work item — Create bug: Checkout fails with 500 for EU customers',
    });
    expect(proposal).toHaveAttribute('href', '/actions/act-1');
    expect(scope.getByRole('link', { name: 'Review actions' })).toHaveAttribute('href', '/actions/act-1');
    expect(scope.getByRole('link', { name: 'Workflow timeline' })).toHaveAttribute(
      'href',
      '/workflows/run-1',
    );
    await user.click(scope.getByRole('link', { name: 'Review actions' }));
    expect(await screen.findByText('Action review')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/actions/act-1');
  });

  it('links to the workflow when there is no open proposal and no analysis', async () => {
    setup({
      workflow: () =>
        detailWith(
          { model_name: null, model_provider: null, current_step: null },
          { analysis: null, actions: [actionSummary({ status: 'superseded' })] },
        ),
    });
    const panel = (await screen.findByText('Latest analysis')).closest('section')!;
    const scope = within(panel);
    expect(scope.getByRole('link', { name: 'Open workflow' })).toHaveAttribute('href', '/workflows/run-1');
    expect(scope.queryByText('Proposed actions')).toBeNull();
    expect(scope.queryByText('Error')).toBeNull();
    expect(scope.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });

  it('polls every 2 s while the run is in flight, then offers re-analysis', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status: WorkflowDetail['run']['status'] = 'classified';
    const { calls } = setup({ workflow: () => detailWith({ status, current_step: 'classify' }) });
    expect(
      await screen.findByText('Analysis in progress — this panel refreshes automatically.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Re-analyze' })).toBeNull();
    expect(callsTo(calls, 'GET', RUN)).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(2000));
    await waitFor(() => expect(callsTo(calls, 'GET', RUN)).toHaveLength(2));

    status = 'pending_approval';
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(await screen.findByRole('button', { name: 'Re-analyze' })).toBeInTheDocument();
    const settled = callsTo(calls, 'GET', RUN).length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(callsTo(calls, 'GET', RUN)).toHaveLength(settled);
  });

  it('offers "Analyze again" for a terminal run', async () => {
    const user = userEvent.setup();
    const { calls } = setup({
      withNotifications: true,
      workflow: () => detailWith({ status: 'completed' }),
      extra: [
        {
          method: 'POST',
          path: '/api/v1/emails/email-1/analyze',
          handler: () => jsonResponse({ workflow_run: workflowRun(), created: true, job_id: 'j' }, 202),
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: 'Analyze again' }));
    await waitFor(() => expect(callsTo(calls, 'POST', '/api/v1/emails/email-1/analyze')).toHaveLength(1));
    expect(screen.queryByRole('button', { name: 'Re-analyze' })).toBeNull();
  });

  it('confirms re-analysis in a modal and closes it after the server accepts', async () => {
    const user = userEvent.setup();
    const hold = deferred<Response>();
    const { calls } = setup({
      withNotifications: true,
      extra: [{ method: 'POST', path: '/api/v1/workflows/run-1/reanalyze', handler: () => hold.promise }],
    });
    await user.click(await screen.findByRole('button', { name: 'Re-analyze' }));
    const dialog = await screen.findByRole('dialog', { name: 'Re-analyze this email?' });
    expect(dialog).toHaveTextContent(/All open proposals of this run/);
    const confirm = within(dialog).getByRole('button', { name: 'Re-analyze' });
    await user.click(confirm);
    await waitFor(() => expect(confirm).toHaveAttribute('data-loading', 'true'));
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(callsTo(calls, 'POST', '/api/v1/workflows/run-1/reanalyze')).toHaveLength(1);
    hold.resolve(jsonResponse({ job_ids: ['j'], message: 'Re-analysis queued.' }, 202));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(await screen.findByText('Re-analysis queued')).toBeInTheDocument();
  });

  it('closes the modal with Cancel without sending anything', async () => {
    const user = userEvent.setup();
    const { calls } = setup();
    await user.click(await screen.findByRole('button', { name: 'Re-analyze' }));
    const dialog = await screen.findByRole('dialog', { name: 'Re-analyze this email?' });
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(callsTo(calls, 'POST', '/api/v1/workflows/run-1/reanalyze')).toHaveLength(0);
  });

  it('shows no analyze/re-analyze controls to a viewer', async () => {
    setup({ session: PERSONAS.viewer });
    await screen.findByText('Latest analysis');
    expect(screen.queryByRole('button', { name: /analyze/i })).toBeNull();
  });
});
