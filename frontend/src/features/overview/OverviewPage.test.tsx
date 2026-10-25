import { act, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { DashboardSummaryOut } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { PERSONAS, auditEvent, dashboardSummary, sessionFor } from '../../test/fixtures';
import {
  callsTo,
  errorResponse,
  jsonResponse,
  mockFetch,
  renderWithProviders,
  textContent,
} from '../../test/utils';
import { ActivityFeed, OverviewPage } from './OverviewPage';

const SUMMARY = '/api/v1/dashboard/summary';

function serve(body: () => DashboardSummaryOut | Response) {
  return mockFetch([
    {
      method: 'GET',
      path: SUMMARY,
      handler: () => {
        const value = body();
        return value instanceof Response ? value : jsonResponse(value);
      },
    },
  ]);
}

function render(session = PERSONAS.approver) {
  return renderWithProviders(<OverviewPage />, {
    session,
    route: '/',
    path: '/',
    extraRoutes: [
      { path: '/inbox', element: <p>inbox page</p> },
      { path: '/approvals', element: <p>approvals page</p> },
      { path: '/integrations', element: <p>integrations page</p> },
      { path: '/audit', element: <p>audit page</p> },
      { path: '/actions/:id', element: <p>action page</p> },
      { path: '/workflows/:id', element: <p>workflow page</p> },
    ],
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('<OverviewPage>', () => {
  it('greets by first name with the tenant eyebrow and shows a KPI skeleton while loading', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => undefined)),
    );
    render();
    expect(screen.getByRole('heading', { level: 1, name: 'Welcome back, Avery' })).toBeInTheDocument();
    expect(screen.getByText('Contoso · operations overview')).toBeInTheDocument();
    expect(screen.getByLabelText('Loading KPIs')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('link', { name: 'Review approvals' })).toHaveAttribute('href', '/approvals');
  });

  it('uses the whole display name when it is a single word', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => undefined)),
    );
    render(sessionFor('member', { display_name: 'Cher' }));
    expect(screen.getByRole('heading', { level: 1, name: 'Welcome back, Cher' })).toBeInTheDocument();
  });

  it('keeps the header on error, shows the support reference and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = serve(() =>
      fail ? errorResponse(503, 'service_unavailable', { correlationId: 'corr-dash' }) : dashboardSummary(),
    );
    render();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('corr-dash');
    expect(alert).toHaveTextContent('service_unavailable, HTTP 503');
    expect(screen.getByRole('heading', { level: 1, name: 'Welcome back, Avery' })).toBeInTheDocument();
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('region', { name: 'Key metrics' })).toBeInTheDocument();
    expect(callsTo(calls, 'GET', SUMMARY)).toHaveLength(2);
  });

  it('shows the empty pipeline state with links when nothing has been synced or proposed', async () => {
    const user = userEvent.setup();
    serve(() => dashboardSummary({ emails_total: 0, actions_proposed: 0 }));
    const { router } = render();
    expect(await screen.findByText('No emails yet — sync your mailbox')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Key metrics' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Integrations' })).toHaveAttribute('href', '/integrations');
    await user.click(screen.getByRole('link', { name: 'Go to inbox' }));
    expect(router.state.location.pathname).toBe('/inbox');
  });

  it('renders KPI tiles, distributions, LLM usage and recent activity; no accessibility violations', async () => {
    const user = userEvent.setup();
    serve(() =>
      dashboardSummary({
        emails_total: 12_900,
        emails_analyzed: 1284,
        workflows_by_status: { pending_approval: 4, completed: 15, failed: 0, legacy_state: 2, gone: 0 },
        categories: { incident: 6, bug: 9, information_only: 10, zero: 0, quantum_request: 1 },
      }),
    );
    const { router } = render();
    const kpis = within(await screen.findByRole('region', { name: 'Key metrics' }));

    const analyzed = kpis.getByRole('link', { name: 'Emails analyzed: 1,284. Open' });
    expect(analyzed).toHaveAttribute('href', '/inbox');
    expect(analyzed).toHaveTextContent('of 12.9K synced');
    expect(kpis.getByRole('link', { name: 'Pending approvals: 4. Open' })).toHaveAttribute(
      'href',
      '/approvals',
    );
    const attention = kpis.getByRole('link', { name: 'Failed / uncertain: 2. Open' });
    expect(attention).toHaveAttribute('href', '/approvals?view=attention');
    expect(attention).toHaveTextContent('1 failed · 1 uncertain');
    // Tiles without a destination are not links.
    expect(kpis.getByText('Actions proposed')).toBeInTheDocument();
    expect(kpis.getByText('Actions proposed').closest('a')).toBeNull();
    expect(kpis.getByText('Completed').closest('a')).toBeNull();
    expect(kpis.getByText('Rejected').closest('a')).toBeNull();
    expect(kpis.getByText('25')).toBeInTheDocument();
    expect(kpis.getByText('15')).toBeInTheDocument();
    expect(kpis.getByText('3')).toBeInTheDocument();

    // Workflows distribution: known statuses in enum order, then unknown keys; zero counts dropped.
    const workflows = screen.getByRole('list', { name: 'Workflows by status' });
    expect(
      within(workflows)
        .getAllByRole('listitem')
        .map((li) => li.getAttribute('aria-label')),
    ).toEqual(['Pending approval: 4 (19%)', 'Completed: 15 (71%)', 'Legacy state: 2 (10%)']);

    // Categories sorted by count, unknown categories humanized, zero counts dropped.
    const categories = screen.getByRole('list', { name: 'Email categories' });
    expect(
      within(categories)
        .getAllByRole('listitem')
        .map((li) => li.getAttribute('aria-label')),
    ).toEqual(['Information only: 10 (38%)', 'Bug: 9 (35%)', 'Incident: 6 (23%)', 'Quantum request: 1 (4%)']);

    expect(screen.getByText('1.5h')).toBeInTheDocument();
    const llm = screen.getByRole('region', { name: 'LLM usage' });
    expect(llm).toHaveTextContent('Tokens125K');
    expect(llm).toHaveTextContent('Est. cost$0.4200');
    expect(llm).toHaveTextContent('Avg latency850 ms');

    const activity = screen.getByRole('region', { name: 'Recent activity' });
    expect(within(activity).getByRole('link', { name: 'Action approved' })).toHaveAttribute(
      'href',
      '/actions/act-1',
    );
    expect(within(activity).getByRole('link', { name: 'Open the audit log' })).toHaveAttribute(
      'href',
      '/audit',
    );

    await expectNoSeriousA11yViolations();

    await user.click(attention);
    expect(router.state.location.pathname).toBe('/approvals');
    expect(router.state.location.search).toBe('?view=attention');
  });

  it('shows dashes for missing LLM metrics and empty distributions', async () => {
    serve(() =>
      dashboardSummary({
        workflows_by_status: {},
        categories: {},
        avg_approval_wait_seconds: null,
        llm: { total_tokens: 0, estimated_cost_usd: null, avg_latency_ms: null },
        recent_activity: [],
      }),
    );
    render();
    expect(await screen.findByText('No workflow runs yet.')).toBeInTheDocument();
    expect(screen.getByText('No classified email yet.')).toBeInTheDocument();
    const llm = screen.getByRole('region', { name: 'LLM usage' });
    expect(llm).toHaveTextContent('Tokens0');
    expect(llm).toHaveTextContent('Est. cost—');
    expect(llm).toHaveTextContent('Avg latency—');
    expect(screen.getByText('Average approval wait').parentElement?.parentElement).toHaveTextContent('—');
    expect(screen.getByText(/No activity yet/)).toBeInTheDocument();
  });

  it('refetches the summary every 15 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let pending = 4;
    const { calls } = serve(() => dashboardSummary({ pending_approvals: pending }));
    render();
    expect(await screen.findByRole('link', { name: 'Pending approvals: 4. Open' })).toBeInTheDocument();
    pending = 7;
    await act(() => vi.advanceTimersByTimeAsync(15_000));
    expect(await screen.findByRole('link', { name: 'Pending approvals: 7. Open' })).toBeInTheDocument();
    expect(callsTo(calls, 'GET', SUMMARY)).toHaveLength(2);
  });
});

describe('<ActivityFeed>', () => {
  it('links to the action, else the workflow, else renders plain text; falls back to the actor type', () => {
    const { container } = renderWithProviders(
      <ActivityFeed
        events={[
          auditEvent({ id: 'a', event_type: 'action.approved', outcome: 'success' }),
          auditEvent({
            id: 'b',
            event_type: 'workflow.failed',
            action_id: null,
            outcome: 'failure',
            actor_name: null,
            actor_type: 'agent',
            resource_type: 'workflow_run',
          }),
          auditEvent({
            id: 'c',
            event_type: 'auth.denied',
            action_id: null,
            workflow_run_id: null,
            outcome: 'denied',
            actor_name: null,
            actor_type: 'system',
            resource_type: 'session',
          }),
        ]}
      />,
    );
    expect(screen.getByRole('link', { name: 'Action approved' })).toHaveAttribute('href', '/actions/act-1');
    expect(screen.getByRole('link', { name: 'Workflow failed' })).toHaveAttribute('href', '/workflows/run-1');
    const denied = screen.getByText('Auth denied');
    expect(denied.closest('a')).toBeNull();
    expect(screen.getByLabelText('Status: Success')).toBeInTheDocument();
    expect(screen.getByLabelText('Status: Failure')).toBeInTheDocument();
    expect(screen.getByLabelText('Status: Denied')).toBeInTheDocument();
    expect(screen.getByText(textContent(/Agent · Workflow run ·/))).toBeInTheDocument();
    expect(screen.getByText(textContent(/System · Session ·/))).toBeInTheDocument();
    expect(screen.getByText(textContent(/Avery Approver · Action ·/))).toBeInTheDocument();
    // Outcome-specific bullet icons.
    expect(container.querySelector('.tabler-icon-circle-check')).not.toBeNull();
    expect(container.querySelector('.tabler-icon-x')).not.toBeNull();
    expect(container.querySelector('.tabler-icon-alert-triangle')).not.toBeNull();
  });

  it('explains how to get started when there is no activity', () => {
    renderWithProviders(<ActivityFeed events={[]} />);
    expect(
      screen.getByText('No activity yet. Sync a mailbox and analyze an email to get started.'),
    ).toBeInTheDocument();
  });
});
