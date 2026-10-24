import { createBrowserRouter, type RouteObject } from 'react-router';

import { LoginPage } from '../features/auth/LoginPage';
import { RequireAuth } from '../features/auth/RequireAuth';
import { AppLayout } from '../features/shell/AppLayout';
import { NotFoundPage, RouteErrorBoundary } from './RouteErrors';

/** Route table. Feature pages are code-split; the shell and login are in the main chunk. */
export const routes: RouteObject[] = [
  { path: '/login', Component: LoginPage, ErrorBoundary: RouteErrorBoundary },
  {
    Component: RequireAuth,
    ErrorBoundary: RouteErrorBoundary,
    children: [
      {
        Component: AppLayout,
        children: [
          {
            index: true,
            lazy: async () => ({
              Component: (await import('../features/overview/OverviewPage')).OverviewPage,
            }),
          },
          {
            path: 'inbox',
            lazy: async () => ({ Component: (await import('../features/inbox/InboxPage')).InboxPage }),
          },
          {
            path: 'emails/:emailId',
            lazy: async () => ({
              Component: (await import('../features/inbox/EmailDetailPage')).EmailDetailPage,
            }),
          },
          {
            path: 'approvals',
            lazy: async () => ({
              Component: (await import('../features/approvals/ApprovalsPage')).ApprovalsPage,
            }),
          },
          {
            path: 'actions/:actionId',
            lazy: async () => ({
              Component: (await import('../features/actions/ActionReviewPage')).ActionReviewPage,
            }),
          },
          {
            path: 'workflows',
            lazy: async () => ({
              Component: (await import('../features/workflows/WorkflowsPage')).WorkflowsPage,
            }),
          },
          {
            path: 'workflows/:workflowId',
            lazy: async () => ({
              Component: (await import('../features/workflows/WorkflowDetailPage')).WorkflowDetailPage,
            }),
          },
          {
            path: 'integrations',
            lazy: async () => ({
              Component: (await import('../features/integrations/IntegrationsPage')).IntegrationsPage,
            }),
          },
          {
            path: 'settings',
            lazy: async () => ({
              Component: (await import('../features/settings/SettingsPage')).SettingsPage,
            }),
          },
          {
            path: 'audit',
            lazy: async () => ({ Component: (await import('../features/audit/AuditLogPage')).AuditLogPage }),
          },
          {
            path: 'tasks',
            lazy: async () => ({ Component: (await import('../features/tasks/TasksPage')).TasksPage }),
          },
          { path: '*', Component: NotFoundPage },
        ],
      },
    ],
  },
];

export function createAppRouter() {
  return createBrowserRouter(routes);
}
