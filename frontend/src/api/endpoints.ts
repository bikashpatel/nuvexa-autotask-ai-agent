/** One typed function per endpoint in docs/api.md. No endpoint outside that table is used. */
import { http } from './client';
import type {
  ActionDetail,
  ActionOut,
  ActionStatus,
  ActionType,
  ActionUpdateRequest,
  AdoConnectionTestOut,
  AdoProjectsOut,
  AdoValidateOut,
  AdoValidateRequest,
  AdoWorkItemTypesOut,
  AnalysisOut,
  ApproveRequest,
  AuditEventOut,
  AuditOutcome,
  AuthConfigOut,
  CancelRequest,
  DashboardSummaryOut,
  DemoPersona,
  EmailDetail,
  EmailListFilters,
  EmailListItem,
  ExecuteAccepted,
  ExecuteRequest,
  ExecutionOut,
  FollowUpTaskOut,
  HealthOut,
  IntegrationsOut,
  JobAccepted,
  MeOut,
  Page,
  RejectRequest,
  ReplyDraftOut,
  SettingsOut,
  SettingsUpdateRequest,
  StartAnalysisOut,
  SyncRequest,
  TaskStatus,
  WorkflowDetail,
  WorkflowEventOut,
  WorkflowRunOut,
  WorkflowStatus,
} from './types';

export interface PageParams {
  limit?: number;
  offset?: number;
}

const enc = encodeURIComponent;

export const healthApi = {
  live: () => http.get<HealthOut>('/health/live', { skipAuthRedirect: true }),
  ready: () => http.get<HealthOut>('/health/ready', { skipAuthRedirect: true }),
};

export const authApi = {
  config: () => http.get<AuthConfigOut>('/auth/config', { skipAuthRedirect: true }),
  me: () => http.get<MeOut>('/auth/me', { skipAuthRedirect: true }),
  demoLogin: (persona: DemoPersona) =>
    http.post<MeOut>('/auth/demo-login', { persona }, { skipAuthRedirect: true }),
  logout: () => http.post<undefined>('/auth/logout', undefined, { skipAuthRedirect: true }),
};

export const integrationsApi = {
  get: () => http.get<IntegrationsOut>('/integrations'),
  disconnect: (connectionId: string) =>
    http.delete<undefined>(`/integrations/connections/${enc(connectionId)}`),
  testAzureDevOps: () => http.get<AdoConnectionTestOut>('/integrations/azure-devops/test'),
  adoProjects: () => http.get<AdoProjectsOut>('/integrations/azure-devops/projects'),
  adoWorkItemTypes: (project: string) =>
    http.get<AdoWorkItemTypesOut>(`/integrations/azure-devops/projects/${enc(project)}/work-item-types`),
  validateAdoPayload: (body: AdoValidateRequest) =>
    http.post<AdoValidateOut>('/integrations/azure-devops/validate', body),
};

export const emailsApi = {
  list: (filters: EmailListFilters) => http.get<Page<EmailListItem>>('/emails', { query: { ...filters } }),
  get: (id: string) => http.get<EmailDetail>(`/emails/${enc(id)}`),
  sync: (body: SyncRequest = {}) => http.post<JobAccepted>('/emails/sync', body),
  analyze: (id: string) => http.post<StartAnalysisOut>(`/emails/${enc(id)}/analyze`),
};

export const workflowsApi = {
  list: (params: PageParams & { status?: WorkflowStatus; email_id?: string }) =>
    http.get<Page<WorkflowRunOut>>('/workflows', { query: { ...params } }),
  get: (id: string) => http.get<WorkflowDetail>(`/workflows/${enc(id)}`),
  analysis: (id: string) => http.get<AnalysisOut>(`/workflows/${enc(id)}/analysis`),
  actions: (id: string) => http.get<ActionOut[]>(`/workflows/${enc(id)}/actions`),
  events: (id: string) => http.get<WorkflowEventOut[]>(`/workflows/${enc(id)}/events`),
  reanalyze: (id: string) => http.post<JobAccepted>(`/workflows/${enc(id)}/reanalyze`),
  cancel: (id: string) => http.post<WorkflowRunOut>(`/workflows/${enc(id)}/cancel`),
};

export const actionsApi = {
  list: (
    params: PageParams & {
      /** One status or several (sent as repeated `status` params). */
      status?: ActionStatus | ActionStatus[];
      action_type?: ActionType;
      workflow_run_id?: string;
    },
  ) => http.get<Page<ActionOut>>('/actions', { query: { ...params } }),
  get: (id: string) => http.get<ActionDetail>(`/actions/${enc(id)}`),
  update: (id: string, body: ActionUpdateRequest) => http.patch<ActionDetail>(`/actions/${enc(id)}`, body),
  approve: (id: string, body: ApproveRequest) => http.post<ActionDetail>(`/actions/${enc(id)}/approve`, body),
  reject: (id: string, body: RejectRequest) => http.post<ActionDetail>(`/actions/${enc(id)}/reject`, body),
  cancel: (id: string, body: CancelRequest) => http.post<ActionDetail>(`/actions/${enc(id)}/cancel`, body),
  execute: (id: string, body: ExecuteRequest) =>
    http.post<ExecuteAccepted>(`/actions/${enc(id)}/execute`, body),
  /** Queued like execute: 202 `ExecuteAccepted`; poll the action afterwards. No request body. */
  reconcile: (id: string) => http.post<ExecuteAccepted>(`/actions/${enc(id)}/reconcile`),
  executions: (id: string) => http.get<ExecutionOut[]>(`/actions/${enc(id)}/executions`),
  history: (id: string) => http.get<AuditEventOut[]>(`/actions/${enc(id)}/history`),
};

export const tasksApi = {
  list: (params: PageParams & { status?: TaskStatus }) =>
    http.get<Page<FollowUpTaskOut>>('/tasks', { query: { ...params } }),
  update: (id: string, status: TaskStatus) => http.patch<FollowUpTaskOut>(`/tasks/${enc(id)}`, { status }),
};

export const replyDraftsApi = {
  list: (params: PageParams) => http.get<Page<ReplyDraftOut>>('/reply-drafts', { query: { ...params } }),
};

export const auditApi = {
  list: (
    params: PageParams & {
      event_type?: string;
      resource_type?: string;
      resource_id?: string;
      action_id?: string;
      outcome?: AuditOutcome;
    },
  ) => http.get<Page<AuditEventOut>>('/audit-events', { query: { ...params } }),
};

export const settingsApi = {
  get: () => http.get<SettingsOut>('/settings'),
  update: (body: SettingsUpdateRequest) => http.put<SettingsOut>('/settings', body),
};

export const dashboardApi = {
  summary: () => http.get<DashboardSummaryOut>('/dashboard/summary'),
};
