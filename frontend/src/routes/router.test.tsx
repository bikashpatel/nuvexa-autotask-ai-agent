import type { ComponentType } from 'react';
import type { RouteObject } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ActionReviewPage } from '../features/actions/ActionReviewPage';
import { ApprovalsPage } from '../features/approvals/ApprovalsPage';
import { AuditLogPage } from '../features/audit/AuditLogPage';
import { LoginPage } from '../features/auth/LoginPage';
import { RequireAuth } from '../features/auth/RequireAuth';
import { EmailDetailPage } from '../features/inbox/EmailDetailPage';
import { InboxPage } from '../features/inbox/InboxPage';
import { IntegrationsPage } from '../features/integrations/IntegrationsPage';
import { OverviewPage } from '../features/overview/OverviewPage';
import { SettingsPage } from '../features/settings/SettingsPage';
import { AppLayout } from '../features/shell/AppLayout';
import { TasksPage } from '../features/tasks/TasksPage';
import { WorkflowDetailPage } from '../features/workflows/WorkflowDetailPage';
import { WorkflowsPage } from '../features/workflows/WorkflowsPage';
import { NotFoundPage, RouteErrorBoundary } from './RouteErrors';
import { createAppRouter, routes } from './router';

const [loginRoute, guardRoute] = routes;
const shellRoute = guardRoute?.children?.[0];
const pageRoutes: RouteObject[] = shellRoute?.children ?? [];

function pageRoute(path: string | undefined): RouteObject {
  const found = pageRoutes.find((r) => (path === undefined ? r.index === true : r.path === path));
  if (!found) throw new Error(`route ${path ?? '(index)'} not found`);
  return found;
}

async function resolveLazy(route: RouteObject): Promise<ComponentType | undefined> {
  const lazy = route.lazy as (() => Promise<{ Component?: ComponentType }>) | undefined;
  if (typeof lazy !== 'function') throw new Error('route is not lazy');
  return (await lazy()).Component;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('route table', () => {
  it('has exactly two top-level routes: the public login page and the auth guard', () => {
    expect(routes).toHaveLength(2);
    expect(loginRoute).toMatchObject({
      path: '/login',
      Component: LoginPage,
      ErrorBoundary: RouteErrorBoundary,
    });
    expect(guardRoute?.path).toBeUndefined();
    expect(guardRoute).toMatchObject({ Component: RequireAuth, ErrorBoundary: RouteErrorBoundary });
  });

  it('puts every protected page inside the app shell, behind the guard', () => {
    expect(guardRoute?.children).toHaveLength(1);
    expect(shellRoute?.Component).toBe(AppLayout);
    expect(pageRoutes.map((r) => (r.index ? '(index)' : r.path))).toEqual([
      '(index)',
      'inbox',
      'emails/:emailId',
      'approvals',
      'actions/:actionId',
      'workflows',
      'workflows/:workflowId',
      'integrations',
      'settings',
      'audit',
      'tasks',
      '*',
    ]);
  });

  it.each<[string | undefined, ComponentType]>([
    [undefined, OverviewPage],
    ['inbox', InboxPage],
    ['emails/:emailId', EmailDetailPage],
    ['approvals', ApprovalsPage],
    ['actions/:actionId', ActionReviewPage],
    ['workflows', WorkflowsPage],
    ['workflows/:workflowId', WorkflowDetailPage],
    ['integrations', IntegrationsPage],
    ['settings', SettingsPage],
    ['audit', AuditLogPage],
    ['tasks', TasksPage],
  ])('lazy-loads the page component for %s', async (path, Component) => {
    const route = pageRoute(path);
    expect(route.Component).toBeUndefined();
    expect(await resolveLazy(route)).toBe(Component);
  });

  it('renders NotFoundPage for unknown paths inside the shell (eagerly, not lazy)', () => {
    const notFound = pageRoute('*');
    expect(notFound.Component).toBe(NotFoundPage);
    expect(notFound.lazy).toBeUndefined();
  });
});

describe('createAppRouter', () => {
  it('creates a browser router over the route table at the current location', () => {
    const router = createAppRouter();
    try {
      expect(router.routes).toHaveLength(2);
      expect(router.routes[0]?.path).toBe('/login');
      expect(router.state.location.pathname).toBe(window.location.pathname);
    } finally {
      router.dispose();
    }
  });
});
