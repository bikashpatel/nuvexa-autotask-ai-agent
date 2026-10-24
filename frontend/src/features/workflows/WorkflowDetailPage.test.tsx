import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AuditEventOut, MeOut, WorkflowDetail } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import {
  PERSONAS,
  XSS_TEXT,
  actionSummary,
  analysis,
  auditEvent,
  workflowDetail,
  workflowEvent,
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
import { WorkflowDetailPage } from './WorkflowDetailPage';

const DETAIL = '/api/v1/workflows/run-1';

function clearCookies() {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

function setup(
  detail: () => WorkflowDetail | Response,
  {
    session = PERSONAS.approver,
    history = {},
    extra = [],
  }: {
    session?: MeOut;
    history?: Record<string, () => AuditEventOut[] | Response | Promise<Response>>;
    extra?: Route[];
  } = {},
) {
  const { calls } = mockFetch([
    {
      method: 'GET',
      path: DETAIL,
      handler: () => {
        const value = detail();
        return value instanceof Response ? value : jsonResponse(value);
      },
    },
    {
      method: 'GET',
      path: /^\/api\/v1\/actions\/[^/]+\/history$/,
      handler: async (call) => {
        const id = call.url.split('/')[4]!;
        const value = await (history[id]?.() ?? []);
        return value instanceof Response ? value : jsonResponse(value);
      },
    },
    ...extra,
  ]);
  const result = renderWithProviders(<WorkflowDetailPage />, {
    session,
    path: '/workflows/:workflowId',
    route: '/workflows/run-1',
    withNotifications: true,
    extraRoutes: [
      { path: '/workflows', element: <p>workflows list</p> },
      { path: '/emails/:emailId', element: <p>email page</p> },
      { path: '/actions/:actionId', element: <p>action page</p> },
    ],
  });
  return { ...result, calls };
}

beforeEach(() => {
  clearCookies();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  clearCookies();
});

describe('<WorkflowDetailPage> states', () => {
  it('shows a skeleton while loading', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => undefined)),
    );
    renderWithProviders(<WorkflowDetailPage />, {
      path: '/workflows/:workflowId',
      route: '/workflows/run-1',
    });
    expect(screen.getByLabelText('Loading')).toHaveAttribute('aria-busy', 'true');
  });

  it('shows the error with its support reference and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = setup(() =>
      fail ? errorResponse(404, 'not_found', { correlationId: 'corr-wf-404' }) : workflowDetail(),
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Not found');
    expect(alert).toHaveTextContent('corr-wf-404');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(
      await screen.findByRole('heading', { level: 1, name: 'URGENT: checkout broken' }),
    ).toBeInTheDocument();
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(2);
  });

  it('shows empty-state copy when nothing has happened yet', async () => {
    setup(() =>
      workflowDetail({
        run: workflowRun({ metrics: {}, email_subject: null, awaiting_review_since: null }),
        analysis: null,
        actions: [],
        events: [],
      }),
    );
    expect(await screen.findByRole('heading', { level: 1, name: 'Workflow run' })).toBeInTheDocument();
    expect(screen.getByText('No events recorded yet.')).toBeInTheDocument();
    expect(screen.getByText('No metrics recorded.')).toBeInTheDocument();
    expect(screen.getByText('No actions proposed (yet).')).toBeInTheDocument();
    expect(screen.getByText('No actions, so no action audit events.')).toBeInTheDocument();
    // Breadcrumb falls back to the run id.
    expect(screen.getByText('run-1')).toBeInTheDocument();
    expect(screen.queryByText('Awaiting review since')).toBeNull();
  });
});

describe('<WorkflowDetailPage> content', () => {
  const richDetail = (): WorkflowDetail =>
    workflowDetail({
      run: workflowRun({
        metrics: {
          total_tokens: 1234,
          score: 0.123456,
          label: 'fast',
          cached: true,
          skipped: false,
          missing: null,
          nested: { a: 1 },
        },
      }),
      analysis: analysis({
        injection_flags: [{ rule: 'ignore_previous_instructions', severity: 'high', excerpt: 'IGNORE' }],
      }),
      actions: [
        actionSummary({
          id: 'act-1',
          status: 'completed',
          external_id: '4711',
          external_url: 'https://dev.azure.com/contoso/Payments/_workitems/edit/4711',
        }),
        actionSummary({
          id: 'act-2',
          action_type: 'propose_email_reply',
          title: 'Reply to Dana',
          version: 2,
          external_id: null,
          external_url: 'mock://contoso/Payments/_workitems/edit/12',
        }),
        actionSummary({ id: 'act-3', title: 'Follow up', action_type: 'create_follow_up_task' }),
      ],
      events: [
        workflowEvent({
          id: 'ev-llm',
          step: 'classify',
          event_type: 'llm_call',
          status: 'succeeded',
          latency_ms: 420,
          details: {
            model: 'gpt-4o',
            provider: 'azure_openai',
            usage: { input_tokens: 800, output_tokens: 120, attempts: 3 },
            estimated_cost_usd: 0.0123,
            retry_count: 2,
          },
        }),
        workflowEvent({
          id: 'ev-tool',
          step: 'plan',
          event_type: 'tool_call',
          status: 'started',
          message: 'Proposal accepted by policy',
          latency_ms: null,
          details: { tool_name: 'propose_azure_devops_work_item', arguments: { title: 'Fix checkout' } },
        }),
        workflowEvent({
          id: 'ev-fail',
          step: 'execute',
          event_type: 'step_failed',
          status: 'error',
          details: { error: { message: 'provider timed out', category: 'timeout' } },
        }),
        workflowEvent({
          id: 'ev-unc',
          step: 'execute',
          event_type: 'integration_call',
          status: 'uncertain',
          details: {},
        }),
        workflowEvent({
          id: 'ev-rej',
          step: 'review',
          event_type: 'resume',
          status: 'rejected',
          details: {},
        }),
        workflowEvent({
          id: 'ev-skip',
          step: 'extract',
          event_type: 'step_completed',
          status: 'skipped',
          details: {},
        }),
        workflowEvent({
          id: 'ev-int',
          step: 'await_review',
          event_type: 'interrupt',
          status: 'waiting',
          details: {},
        }),
        workflowEvent({
          id: 'ev-other',
          step: 'normalize',
          event_type: 'status_changed',
          status: 'noted',
          details: {},
        }),
      ],
    });

  it('renders header, stepper, timeline facts, run facts, metrics and actions; no accessibility violations', async () => {
    const user = userEvent.setup();
    const { router } = setup(richDetail, {
      history: {
        'act-1': () => [
          auditEvent({ id: 'h1', created_at: '2026-10-24T10:05:00Z', event_type: 'action.approved' }),
        ],
        'act-2': () => [
          auditEvent({
            id: 'h2',
            action_id: 'act-2',
            created_at: '2026-10-24T11:00:00Z',
            event_type: 'action.executed',
            actor_name: null,
            actor_type: 'system',
          }),
          auditEvent({
            id: 'h3',
            action_id: 'act-gone',
            created_at: '2026-10-24T09:00:00Z',
            event_type: 'action.created',
          }),
          auditEvent({
            id: 'h4',
            action_id: null,
            created_at: '2026-10-24T08:00:00Z',
            event_type: 'run.noted',
            outcome: 'denied',
          }),
        ],
      },
    });

    expect(
      await screen.findByRole('heading', { level: 1, name: 'URGENT: checkout broken' }),
    ).toBeInTheDocument();
    expect(screen.getByText('Workflow run · attempt #1 · Manual trigger')).toBeInTheDocument();
    expect(screen.getAllByLabelText('Status: Pending approval').length).toBeGreaterThan(0);
    expect(screen.getByText('Injection indicators: 1')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Workflows' })).toHaveAttribute('href', '/workflows');
    expect(screen.getByRole('link', { name: 'Open email' })).toHaveAttribute('href', '/emails/email-1');

    // Stepper: the current step is marked for assistive tech.
    const stepper = screen.getByLabelText('Workflow progress');
    const current = stepper.querySelector('[aria-current="step"]');
    expect(current).toHaveTextContent('Pending approval');
    expect(current).toHaveTextContent('Waiting for a human');

    // LLM call facts.
    const llm = screen.getByText('Classify').closest('.mantine-Timeline-item') as HTMLElement;
    expect(llm).toHaveTextContent('Llm call');
    expect(llm).toHaveTextContent('420 ms');
    expect(llm).toHaveTextContent('Model azure_openai / gpt-4o');
    expect(llm).toHaveTextContent('Tokens 920 (800 in / 120 out)');
    expect(llm).toHaveTextContent('Est. cost $0.0123');
    expect(llm).toHaveTextContent('Attempts 3');
    expect(llm).toHaveTextContent('Retries 2');
    expect(llm.getAttribute('style')).toContain('--mantine-color-green-filled');

    // Tool call: message, tool and sanitized arguments.
    const tool = screen
      .getByText('Proposal accepted by policy')
      .closest('.mantine-Timeline-item') as HTMLElement;
    expect(tool).toHaveTextContent('Tool propose_azure_devops_work_item');
    expect(tool).toHaveTextContent('Sanitized arguments');
    expect(tool).toHaveTextContent('"title": "Fix checkout"');
    expect(tool.getAttribute('style')).toContain('--mantine-color-cyan-filled');

    // Failure: error line with category.
    expect(screen.getByText('Timeout: provider timed out')).toBeInTheDocument();

    // Colour language per status (colour is backed by the status text badge).
    const colourOf = (status: string) =>
      (screen.getByText(status).closest('.mantine-Timeline-item') as HTMLElement).getAttribute('style');
    expect(colourOf('error')).toContain('red');
    expect(colourOf('uncertain')).toContain('grape');
    expect(colourOf('rejected')).toContain('orange');
    expect(colourOf('skipped')).toContain('gray');
    expect(colourOf('waiting')).toContain('orange');
    expect(colourOf('noted')).toContain('gray');

    // Details toggle.
    const toggle = within(llm).getByRole('button', { name: 'Show details' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await user.click(toggle);
    expect(within(llm).getByRole('button', { name: 'Hide details' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(llm).toHaveTextContent('"estimated_cost_usd": 0.0123');
    // Events without details have no toggle.
    const other = screen.getByText('noted').closest('.mantine-Timeline-item') as HTMLElement;
    expect(within(other).queryByRole('button')).toBeNull();

    // Run facts.
    const run = screen.getByRole('region', { name: 'Run' });
    expect(run).toHaveTextContent('Await review');
    expect(run).toHaveTextContent('heuristic / heuristic-demo');
    expect(run).toHaveTextContent('Prompt versionp1');
    expect(run).toHaveTextContent('Schema versions1');
    expect(run).toHaveTextContent('Awaiting review since');
    expect(run).toHaveTextContent('corr-run-1');

    // Metrics formatting.
    const metrics = within(screen.getByRole('region', { name: 'Metrics' }));
    expect(metrics.getByText('Total tokens').nextSibling).toHaveTextContent('1,234');
    expect(metrics.getByText('Score').nextSibling).toHaveTextContent('0.1235');
    expect(metrics.getByText('Label').nextSibling).toHaveTextContent('fast');
    expect(metrics.getByText('Cached').nextSibling).toHaveTextContent('Yes');
    expect(metrics.getByText('Skipped').nextSibling).toHaveTextContent('No');
    expect(metrics.getByText('Missing').nextSibling).toHaveTextContent('—');
    expect(metrics.getByText('Nested').nextSibling).toHaveTextContent('{"a":1}');

    // Actions table: https → safe new-tab link; mock:// → text, never a link.
    const actions = within(screen.getByRole('table', { name: 'Proposed actions' }));
    const external = actions.getByRole('link', { name: /#4711/ });
    expect(external).toHaveAttribute('href', 'https://dev.azure.com/contoso/Payments/_workitems/edit/4711');
    expect(external).toHaveAttribute('target', '_blank');
    expect(external).toHaveAttribute('rel', 'noopener noreferrer');
    const mockUrl = actions.getByText('mock://contoso/Payments/_workitems/edit/12');
    expect(mockUrl.closest('a')).toBeNull();
    expect(actions.getByText('Mock')).toBeInTheDocument();
    expect(actions.getByText('v2')).toBeInTheDocument();
    expect(actions.getByRole('link', { name: 'Reply to Dana' })).toHaveAttribute('href', '/actions/act-2');

    // Audit trail: every action's history merged, newest first.
    const trail = await screen.findByRole('table', { name: "Audit trail for this run's actions" });
    const rows = within(trail).getAllByRole<HTMLTableRowElement>('row').slice(1);
    expect(rows.map((r) => r.cells[1]?.textContent)).toEqual([
      'Action executed',
      'Action approved',
      'Action created',
      'Run noted',
    ]);
    expect(rows[0]).toHaveTextContent('System');
    expect(rows[0]).toHaveTextContent('Reply to Dana');
    expect(rows[2]).toHaveTextContent('act-gone');
    expect(within(rows[3]!).getByLabelText('Status: Denied')).toBeInTheDocument();

    await expectNoSeriousA11yViolations();

    await user.click(actions.getByRole('link', { name: 'Reply to Dana' }));
    expect(router.state.location.pathname).toBe('/actions/act-2');
  });

  it('shows the run failure alert with its category', async () => {
    setup(() =>
      workflowDetail({
        run: workflowRun({
          status: 'failed',
          current_step: 'classify',
          error_category: 'llm_output',
          error_message: 'Model output invalid',
        }),
      }),
    );
    const alert = await screen.findByText('Run failed: Llm output');
    expect(alert.closest('[role="alert"]')).toHaveTextContent('Model output invalid');
    expect(screen.getByText('Classified — Failed')).toBeInTheDocument();
    expect(screen.getByText('Stopped: failed')).toBeInTheDocument();
  });

  it('renders sparse or forward-compatible payloads defensively', async () => {
    setup(() =>
      workflowDetail({
        run: workflowRun({
          status: 'failed',
          error_category: null,
          error_message: 'Worker crashed',
          model_provider: null,
          model_name: null,
          prompt_version: null,
          schema_version: null,
          correlation_id: null,
          metrics: null as unknown as Record<string, unknown>,
        }),
        events: [
          workflowEvent({
            id: 'ev-new',
            step: 'summarize',
            event_type: 'brand_new_type' as never,
            status: 'done',
            details: null as unknown as Record<string, unknown>,
          }),
          workflowEvent({
            id: 'ev-total',
            step: 'classify',
            status: 'ok',
            details: { total_tokens: 77, error_category: 'rate_limited' },
          }),
        ],
      }),
    );
    expect(await screen.findByText('Run failed')).toBeInTheDocument();
    expect(screen.getByText('Worker crashed')).toBeInTheDocument();
    const run = screen.getByRole('region', { name: 'Run' });
    expect(within(run).getByText('Model').nextSibling).toHaveTextContent('—');
    expect(within(run).getByText('Prompt version').nextSibling).toHaveTextContent('—');
    expect(within(run).getByText('Schema version').nextSibling).toHaveTextContent('—');
    expect(within(run).getByText('Correlation id').nextSibling).toHaveTextContent('—');
    expect(screen.getByText('No metrics recorded.')).toBeInTheDocument();
    const unknown = screen.getByText('Summarize').closest('.mantine-Timeline-item') as HTMLElement;
    expect(unknown).toHaveTextContent('Brand new type');
    expect(within(unknown).queryByRole('button')).toBeNull();
    const total = screen.getByText('ok').closest('.mantine-Timeline-item') as HTMLElement;
    expect(total).toHaveTextContent('Tokens 77');
    expect(total).not.toHaveTextContent(' in / ');
    expect(total).toHaveTextContent('Rate limited:');
  });

  it('shows a generic failure alert when only the category is known', async () => {
    setup(() =>
      workflowDetail({
        run: workflowRun({ status: 'failed', error_category: 'internal', error_message: null }),
      }),
    );
    expect(await screen.findByText('Run failed: Internal')).toBeInTheDocument();
    expect(screen.getByText('No details.')).toBeInTheDocument();
  });

  it.each([
    ['rejected', 'Pending approval — Rejected', 'Stopped: rejected'],
    ['cancelled', 'Pending approval — Cancelled', 'Stopped: cancelled'],
  ] as const)('labels the stopped step of a %s run', async (status, label, description) => {
    setup(() => workflowDetail({ run: workflowRun({ status }) }));
    const step = (await screen.findByText(label)).closest('button')!;
    expect(step).toHaveTextContent(description);
    expect(step).toHaveAttribute('aria-current', 'step');
  });

  it('renders untrusted event and subject text inertly', async () => {
    const { container } = setup(() =>
      workflowDetail({
        run: workflowRun({ email_subject: XSS_TEXT }),
        events: [
          workflowEvent({
            message: XSS_TEXT,
            details: { error: XSS_TEXT, tool_name: XSS_TEXT, arguments: { body: XSS_TEXT } },
          }),
        ],
      }),
    );
    expect(await screen.findByRole('heading', { level: 1, name: XSS_TEXT })).toBeInTheDocument();
    expect(screen.getAllByText(XSS_TEXT).length).toBeGreaterThan(1);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });
});

describe('<WorkflowDetailPage> audit trail states', () => {
  it('shows a skeleton while histories load, then the empty copy', async () => {
    const pending = deferred<Response>();
    const { container } = setup(() => workflowDetail(), { history: { 'act-1': () => pending.promise } });
    const trail = await screen.findByRole('region', { name: 'Audit trail' });
    expect(trail.querySelector('.mantine-Skeleton-root')).not.toBeNull();
    pending.resolve(jsonResponse([]));
    expect(await within(trail).findByText('No audit events yet.')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="Audit trail for this run\'s actions"]')).toBeNull();
  });

  it('shows the error and retries every action history', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = setup(
      () => workflowDetail({ actions: [actionSummary({ id: 'act-1' }), actionSummary({ id: 'act-2' })] }),
      {
        history: {
          'act-1': () => [auditEvent()],
          'act-2': () => (fail ? errorResponse(500, 'internal_error', { correlationId: 'corr-hist' }) : []),
        },
      },
    );
    const trail = await screen.findByRole('region', { name: 'Audit trail' });
    const alert = await within(trail).findByRole('alert');
    expect(alert).toHaveTextContent('corr-hist');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await within(trail).findByRole('table')).toBeInTheDocument();
    expect(callsTo(calls, 'GET', '/api/v1/actions/act-1/history')).toHaveLength(2);
    expect(callsTo(calls, 'GET', '/api/v1/actions/act-2/history')).toHaveLength(2);
  });
});

describe('<WorkflowDetailPage> run controls', () => {
  it('offers re-analysis and cancel to users with workflow:run on an open run', async () => {
    setup(() => workflowDetail());
    expect(await screen.findByRole('button', { name: 'Request re-analysis' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel run' })).toBeInTheDocument();
  });

  it('hides re-analysis while the run is in flight but still allows cancelling', async () => {
    setup(() => workflowDetail({ run: workflowRun({ status: 'classified', current_step: 'classify' }) }));
    expect(await screen.findByRole('button', { name: 'Cancel run' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Request re-analysis' })).toBeNull();
  });

  it.each(['completed', 'rejected', 'cancelled'] as const)(
    'offers no run controls on a %s run',
    async (status) => {
      setup(() => workflowDetail({ run: workflowRun({ status }) }));
      expect(await screen.findByRole('link', { name: 'Open email' })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Request re-analysis' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Cancel run' })).toBeNull();
    },
  );

  it('shows the viewer no mutating controls', async () => {
    setup(() => workflowDetail(), { session: PERSONAS.viewer });
    expect(await screen.findByRole('link', { name: 'Open email' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Request re-analysis' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Cancel run' })).toBeNull();
  });

  it('requests a re-analysis after confirmation', async () => {
    const user = userEvent.setup();
    const { calls } = setup(() => workflowDetail(), {
      extra: [
        {
          method: 'POST',
          path: '/api/v1/workflows/run-1/reanalyze',
          handler: () => jsonResponse({ job_ids: ['job-1'], message: 'Re-analysis queued.' }, 202),
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: 'Request re-analysis' }));
    const dialog = await screen.findByRole('dialog', { name: 'Request re-analysis?' });
    expect(dialog).toHaveTextContent('Open proposals of this run are superseded');
    await user.click(within(dialog).getByRole('button', { name: 'Re-analyze' }));
    expect(await screen.findByText('Re-analysis queued')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(callsTo(calls, 'POST', '/api/v1/workflows/run-1/reanalyze')).toHaveLength(1);
  });

  it('cancels the run after confirmation (Escape closes the dialog first)', async () => {
    const user = userEvent.setup();
    document.cookie = 'nuvexa_csrf=csrf-wf; path=/';
    let run = workflowRun();
    const { calls } = setup(() => workflowDetail({ run }), {
      extra: [
        {
          method: 'POST',
          path: '/api/v1/workflows/run-1/cancel',
          handler: () => {
            run = workflowRun({ status: 'cancelled' });
            return jsonResponse(run);
          },
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: 'Cancel run' }));
    expect(await screen.findByRole('dialog', { name: 'Cancel this workflow run?' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(callsTo(calls, 'POST', '/api/v1/workflows/run-1/cancel')).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: 'Cancel run' }));
    const dialog = await screen.findByRole('dialog', { name: 'Cancel this workflow run?' });
    expect(dialog).toHaveTextContent('Completed actions are not affected.');
    const gets = callsTo(calls, 'GET', DETAIL).length;
    await user.click(within(dialog).getByRole('button', { name: 'Cancel run' }));

    expect(await screen.findByText('Workflow cancelled')).toBeInTheDocument();
    const post = callsTo(calls, 'POST', '/api/v1/workflows/run-1/cancel');
    expect(post).toHaveLength(1);
    expect(post[0]!.headers.get('X-CSRF-Token')).toBe('csrf-wf');
    // The run is refetched (invalidated) and the confirmed state shown.
    expect(await screen.findByLabelText('Status: Cancelled')).toBeInTheDocument();
    expect(callsTo(calls, 'GET', DETAIL).length).toBeGreaterThan(gets);
    expect(screen.queryByRole('button', { name: 'Cancel run' })).toBeNull();
  });

  it('shows an error toast with the support reference when cancelling fails', async () => {
    const user = userEvent.setup();
    setup(() => workflowDetail(), {
      extra: [
        {
          method: 'POST',
          path: '/api/v1/workflows/run-1/cancel',
          handler: () =>
            errorResponse(409, 'invalid_state', {
              message: 'Run already finished.',
              correlationId: 'corr-cancel',
            }),
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: 'Cancel run' }));
    const dialog = await screen.findByRole('dialog', { name: 'Cancel this workflow run?' });
    await user.click(within(dialog).getByRole('button', { name: 'Cancel run' }));
    expect(await screen.findByText('Could not cancel the workflow')).toBeInTheDocument();
    expect(screen.getByText('Run already finished.')).toBeInTheDocument();
    expect(screen.getByText('invalid_state · ref corr-cancel')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    // Not optimistic: the run status is unchanged.
    expect(screen.getAllByLabelText('Status: Pending approval').length).toBeGreaterThan(0);
  });
});

describe('<WorkflowDetailPage> polling', () => {
  it('polls every 2 s while the run is in flight and stops once it settles', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let detail = workflowDetail({ run: workflowRun({ status: 'extracted', current_step: 'extract' }) });
    const { calls } = setup(() => detail);
    expect(await screen.findByLabelText('Status: Extracted')).toBeInTheDocument();
    const count = () => callsTo(calls, 'GET', DETAIL).length;
    expect(count()).toBe(1);
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(count()).toBe(2);
    detail = workflowDetail({ run: workflowRun({ status: 'completed', current_step: null }) });
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(await screen.findByLabelText('Status: Completed')).toBeInTheDocument();
    const settled = count();
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(count()).toBe(settled);
  });

  it('polls while an action of the run is executing', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let detail = workflowDetail({
      run: workflowRun({ status: 'approved' }),
      actions: [actionSummary({ status: 'executing' })],
    });
    const { calls } = setup(() => detail);
    expect(await screen.findByLabelText('Status: Executing')).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(2);
    detail = workflowDetail({
      run: workflowRun({ status: 'completed' }),
      actions: [actionSummary({ status: 'completed' })],
    });
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect((await screen.findAllByLabelText('Status: Completed')).length).toBe(2);
    const settled = callsTo(calls, 'GET', DETAIL).length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(settled);
  });

  it('does not poll a run waiting for approval', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = setup(() => workflowDetail());
    expect(await screen.findByText('Event timeline')).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(1);
  });
});
