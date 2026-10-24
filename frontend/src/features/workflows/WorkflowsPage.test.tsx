import { act, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Page, WorkflowRunOut } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { XSS_TEXT, page, workflowRun } from '../../test/fixtures';
import {
  callsTo,
  errorResponse,
  jsonResponse,
  mockFetch,
  queryOf,
  renderWithProviders,
} from '../../test/utils';
import { WorkflowsPage } from './WorkflowsPage';

const LIST = '/api/v1/workflows';

function serve(body: () => Page<WorkflowRunOut> | Response) {
  return mockFetch([
    {
      method: 'GET',
      path: LIST,
      handler: () => {
        const value = body();
        return value instanceof Response ? value : jsonResponse(value);
      },
    },
  ]);
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('<WorkflowsPage>', () => {
  it('shows a loading skeleton while the list is pending', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => undefined)),
    );
    renderWithProviders(<WorkflowsPage />);
    expect(screen.getByRole('heading', { level: 1, name: 'Workflows' })).toBeInTheDocument();
    expect(screen.getByLabelText('Loading workflows')).toHaveAttribute('aria-busy', 'true');
  });

  it('renders runs with links, trigger, step, attempt and model; no accessibility violations', async () => {
    serve(() =>
      page([
        workflowRun(),
        workflowRun({
          id: 'run-2',
          email_subject: null,
          status: 'completed',
          trigger: 'auto',
          current_step: null,
          analysis_attempt: 2,
          model_provider: null,
          model_name: null,
        }),
      ]),
    );
    const { router } = renderWithProviders(<WorkflowsPage />, {
      route: '/workflows',
      path: '/workflows',
      extraRoutes: [{ path: '/workflows/:id', element: <p>workflow detail</p> }],
    });
    const table = await screen.findByRole('table', { name: 'Workflow runs' });
    const rows = within(table).getAllByRole('row');
    expect(rows).toHaveLength(3);

    const first = rows[1]!;
    expect(within(first).getByRole('link', { name: 'URGENT: checkout broken' })).toHaveAttribute(
      'href',
      '/workflows/run-1',
    );
    expect(within(first).getByText('Manual trigger')).toBeInTheDocument();
    expect(within(first).getByLabelText('Status: Pending approval')).toBeInTheDocument();
    expect(within(first).getByText('Await review')).toBeInTheDocument();
    expect(within(first).getByText('#1')).toBeInTheDocument();
    expect(within(first).getByText('heuristic / heuristic-demo')).toBeInTheDocument();

    // Subject falls back to the run id; missing model → em dash; missing step → em dash.
    const second = rows[2]!;
    expect(within(second).getByRole('link', { name: 'run-2' })).toHaveAttribute('href', '/workflows/run-2');
    expect(within(second).getByText('Auto trigger')).toBeInTheDocument();
    expect(within(second).getAllByText('—')).toHaveLength(2);
    expect(screen.getByText('1–2 of 2')).toBeInTheDocument();

    await expectNoSeriousA11yViolations();

    await userEvent.click(within(first).getByRole('link', { name: 'URGENT: checkout broken' }));
    expect(router.state.location.pathname).toBe('/workflows/run-1');
  });

  it('renders untrusted subjects as text', async () => {
    serve(() => page([workflowRun({ email_subject: XSS_TEXT })]));
    const { container } = renderWithProviders(<WorkflowsPage />);
    expect(await screen.findByRole('link', { name: XSS_TEXT })).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });

  it('filters by status through the URL and the request query', async () => {
    const user = userEvent.setup();
    const { calls } = serve(() => page([]));
    const { router } = renderWithProviders(<WorkflowsPage />, { route: '/workflows?offset=25' });
    expect(await screen.findByText('No workflow runs yet')).toBeInTheDocument();
    expect(queryOf(callsTo(calls, 'GET', LIST)[0]!).get('offset')).toBe('25');

    await user.click(screen.getByRole('combobox', { name: 'Status' }));
    await user.click(await screen.findByRole('option', { name: 'Failed' }));

    expect(router.state.location.search).toBe('?status=failed');
    expect(await screen.findByText('No workflows with this status')).toBeInTheDocument();
    const last = callsTo(calls, 'GET', LIST).at(-1)!;
    expect(queryOf(last).get('status')).toBe('failed');
    // Changing a filter resets the page.
    expect(queryOf(last).get('offset')).toBe('0');
    expect(screen.getByText('Analyze an email from the inbox to start a workflow.')).toBeInTheDocument();
  });

  it('ignores an unknown status parameter', async () => {
    const { calls } = serve(() => page([]));
    renderWithProviders(<WorkflowsPage />, { route: '/workflows?status=bogus' });
    expect(await screen.findByText('No workflow runs yet')).toBeInTheDocument();
    expect(queryOf(callsTo(calls, 'GET', LIST)[0]!).has('status')).toBe(false);
    expect(screen.getByRole('combobox', { name: 'Status' })).toHaveValue('');
  });

  it('paginates through the offset parameter', async () => {
    const user = userEvent.setup();
    const runs = Array.from({ length: 25 }, (_, i) =>
      workflowRun({ id: `run-${i}`, email_subject: `S${i}` }),
    );
    // A missing/zero limit in the response falls back to the page size.
    const { calls } = serve(() => page(runs, { total: 60, limit: 0 }));
    const { router } = renderWithProviders(<WorkflowsPage />);
    expect(await screen.findByText('1–25 of 60')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Page 2' }));
    expect(router.state.location.search).toBe('?offset=25');
    expect(queryOf(callsTo(calls, 'GET', LIST).at(-1)!).get('offset')).toBe('25');
    await user.click(screen.getByRole('button', { name: 'Page 1' }));
    expect(router.state.location.search).toBe('');
  });

  it('shows the error with its support reference and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = serve(() =>
      fail ? errorResponse(500, 'internal_error', { correlationId: 'corr-wf-500' }) : page([workflowRun()]),
    );
    renderWithProviders(<WorkflowsPage />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('corr-wf-500');
    expect(alert).toHaveTextContent('internal_error, HTTP 500');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('link', { name: 'URGENT: checkout broken' })).toBeInTheDocument();
    expect(callsTo(calls, 'GET', LIST)).toHaveLength(2);
  });

  it('polls every 2 s while a run is in flight and stops once none is', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status: WorkflowRunOut['status'] = 'classified';
    const { calls } = serve(() => page([workflowRun({ status })]));
    renderWithProviders(<WorkflowsPage />);
    expect(await screen.findByLabelText('Status: Classified')).toBeInTheDocument();
    const count = () => callsTo(calls, 'GET', LIST).length;
    expect(count()).toBe(1);

    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(count()).toBe(2);
    status = 'pending_approval';
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(await screen.findByLabelText('Status: Pending approval')).toBeInTheDocument();
    const settled = count();
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(count()).toBe(settled);
  });

  it('does not poll when nothing is in flight', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = serve(() => page([workflowRun({ status: 'completed' })]));
    renderWithProviders(<WorkflowsPage />);
    expect(await screen.findByLabelText('Status: Completed')).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(callsTo(calls, 'GET', LIST)).toHaveLength(1);
  });
});
