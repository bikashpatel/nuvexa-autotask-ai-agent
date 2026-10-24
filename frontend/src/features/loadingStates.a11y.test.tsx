/**
 * Regression: every loading skeleton is announced as a named, busy `status` region. Before the fix
 * several pages put `aria-label` on a role-less <div> (axe `aria-prohibited-attr`, serious): screen
 * readers ignored the label, so "loading" was never announced.
 */
import { screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { adoAction } from '../test/fixtures';
import { jsonResponse, mockFetch, renderWithProviders, type Route } from '../test/utils';
import { ActionCard } from './actions/ActionCard';
import { ActionReviewPage } from './actions/ActionReviewPage';
import { ApprovalsPage } from './approvals/ApprovalsPage';
import { AuditLogPage } from './audit/AuditLogPage';
import { LoginPage } from './auth/LoginPage';
import { EmailDetailPage } from './inbox/EmailDetailPage';
import { InboxPage } from './inbox/InboxPage';
import { IntegrationsPage } from './integrations/IntegrationsPage';
import { OverviewPage } from './overview/OverviewPage';
import { SettingsPage } from './settings/SettingsPage';
import { TasksPage } from './tasks/TasksPage';
import { WorkflowDetailPage } from './workflows/WorkflowDetailPage';
import { WorkflowsPage } from './workflows/WorkflowsPage';

/** Every request stays in flight, except the ones explicitly answered. */
function holdAllRequests(answered: Route[] = []) {
  const never = new Promise<Response>(() => undefined);
  mockFetch([...answered, { method: 'GET', path: /.*/, handler: () => never }]);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const CASES: { name: string; ui: ReactNode; label: string; path?: string; route?: string }[] = [
  { name: 'Overview', ui: <OverviewPage />, label: 'Loading KPIs' },
  { name: 'Inbox', ui: <InboxPage />, label: 'Loading emails' },
  {
    name: 'Email detail',
    ui: <EmailDetailPage />,
    label: 'Loading',
    path: '/emails/:emailId',
    route: '/emails/email-1',
  },
  { name: 'Approvals', ui: <ApprovalsPage />, label: 'Loading approvals' },
  { name: 'Workflows', ui: <WorkflowsPage />, label: 'Loading workflows' },
  {
    name: 'Workflow detail',
    ui: <WorkflowDetailPage />,
    label: 'Loading',
    path: '/workflows/:workflowId',
    route: '/workflows/run-1',
  },
  { name: 'Integrations', ui: <IntegrationsPage />, label: 'Loading integrations' },
  { name: 'Settings', ui: <SettingsPage />, label: 'Loading settings' },
  { name: 'Audit log', ui: <AuditLogPage />, label: 'Loading audit events' },
  { name: 'Tasks', ui: <TasksPage />, label: 'Loading tasks' },
  { name: 'Reply drafts', ui: <TasksPage />, label: 'Loading reply drafts', route: '/?tab=drafts' },
  { name: 'Action card', ui: <ActionCard actionId="act-1" />, label: 'Loading action' },
];

describe('loading states are announced accessibly', () => {
  it.each(CASES)('$name: named busy status region, no serious axe violations', async (c) => {
    holdAllRequests();
    renderWithProviders(c.ui, { path: c.path, route: c.route });
    const region = await screen.findByRole('status', { name: c.label });
    expect(region).toHaveAttribute('aria-busy', 'true');
    await expectNoSeriousA11yViolations();
  });

  it('Login: sign-in options skeleton is a named busy status region', async () => {
    holdAllRequests();
    renderWithProviders(<LoginPage />, { session: null });
    const region = await screen.findByRole('status', { name: 'Loading sign-in options' });
    expect(region).toHaveAttribute('aria-busy', 'true');
    await expectNoSeriousA11yViolations();
  });

  it('Action review: the original email skeleton is a named busy status region', async () => {
    holdAllRequests([
      { method: 'GET', path: '/api/v1/actions/act-1', handler: () => jsonResponse(adoAction()) },
    ]);
    renderWithProviders(<ActionReviewPage />, { path: '/actions/:actionId', route: '/actions/act-1' });
    const region = await screen.findByRole('status', { name: 'Loading email' });
    expect(region).toHaveAttribute('aria-busy', 'true');
    await expectNoSeriousA11yViolations();
  });
});
