/**
 * Realistic API payloads shaped exactly like app/schemas/api.py (UUIDs/datetimes as strings).
 * Every factory accepts overrides so a test states only what it cares about.
 */
import type {
  ActionDetail,
  ActionOut,
  ActionPermissions,
  AnalysisOut,
  ApprovalOut,
  AuditEventOut,
  AuthConfigOut,
  DashboardSummaryOut,
  EmailDetail,
  EmailListItem,
  ExecutionOut,
  FollowUpTaskOut,
  IntegrationsOut,
  MeOut,
  Page,
  ReplyDraftOut,
  SettingsOut,
  UserRole,
  WorkflowDetail,
  WorkflowEventOut,
  WorkflowRunOut,
} from '../api/types';

export const HASH_V3 = 'a'.repeat(64);
export const HASH_V4 = 'b'.repeat(64);

/** Hostile strings used to prove untrusted text is never interpreted as HTML. */
export const XSS_IMG = '<img src=x onerror="window.__nuvexaXss=1">';
export const XSS_SCRIPT = '<script>window.__nuvexaXss=1</script>';
export const XSS_TEXT = `${XSS_IMG} ${XSS_SCRIPT}`;

// --- Sessions (permission sets mirror app/services/authz.py ROLE_PERMISSIONS) ------------------

const MEMBER_PERMISSIONS = [
  'email:read',
  'email:sync',
  'workflow:run',
  'action:edit',
  'action:approve',
  'action:execute',
];
const APPROVER_PERMISSIONS = [
  ...MEMBER_PERMISSIONS,
  'action:approve_any',
  'audit:read_all',
  'email:read_all',
];

const ROLE_PERMISSIONS: Record<UserRole, string[]> = {
  viewer: ['email:read'],
  member: MEMBER_PERMISSIONS,
  approver: APPROVER_PERMISSIONS,
  admin: [...APPROVER_PERMISSIONS, 'settings:write'],
};

const DISPLAY_NAMES: Record<UserRole, string> = {
  viewer: 'Vic Viewer',
  member: 'Morgan Member',
  approver: 'Avery Approver',
  admin: 'Ada Admin',
};

export function sessionFor(role: UserRole, overrides: Partial<MeOut['user']> = {}): MeOut {
  return {
    user: {
      id: `u-${role}`,
      email: `${role}@contoso.example`,
      display_name: DISPLAY_NAMES[role],
      role,
      tenant_id: 't-1',
      tenant_name: 'Contoso',
      ...overrides,
    },
    permissions: [...ROLE_PERMISSIONS[role]],
  };
}

export const PERSONAS: Record<UserRole, MeOut> = {
  viewer: sessionFor('viewer'),
  member: sessionFor('member'),
  approver: sessionFor('approver'),
  admin: sessionFor('admin'),
};

export function authConfig(overrides: Partial<AuthConfigOut> = {}): AuthConfigOut {
  return {
    app_name: 'NUVEXA AutoTask',
    environment: 'development',
    demo_mode: true,
    demo_login_enabled: true,
    entra_login_enabled: false,
    ...overrides,
  };
}

// --- Generic -------------------------------------------------------------------------------------

export function page<T>(items: T[], overrides: Partial<Page<T>> = {}): Page<T> {
  return { items, total: items.length, limit: 25, offset: 0, ...overrides };
}

// --- Actions -------------------------------------------------------------------------------------

export const ALL_ACTION_PERMISSIONS: ActionPermissions = {
  can_edit: true,
  can_approve: true,
  can_reject: true,
  can_cancel: true,
  can_execute: true,
  can_reconcile: true,
};

export const NO_ACTION_PERMISSIONS: ActionPermissions = {
  can_edit: false,
  can_approve: false,
  can_reject: false,
  can_cancel: false,
  can_execute: false,
  can_reconcile: false,
};

export function approvalFixture(overrides: Partial<ApprovalOut> = {}): ApprovalOut {
  return {
    id: 'ap-1',
    decision: 'approved',
    decided_by_user_id: 'u-approver',
    decided_by_name: 'Avery Approver',
    action_version: 3,
    payload_hash: HASH_V3,
    reason: null,
    created_at: '2026-10-24T10:05:00Z',
    invalidated_at: null,
    invalidated_reason: null,
    ...overrides,
  };
}

export function executionFixture(overrides: Partial<ExecutionOut> = {}): ExecutionOut {
  return {
    id: 'ex-1',
    attempt_number: 1,
    status: 'succeeded',
    deduplicated: false,
    requested_by_user_id: 'u-approver',
    action_version: 3,
    started_at: '2026-10-24T10:20:00Z',
    finished_at: '2026-10-24T10:20:02Z',
    latency_ms: 2100,
    retry_count: 0,
    external_id: '4711',
    external_url: 'https://dev.azure.com/contoso/Payments/_workitems/edit/4711',
    provider_status_code: 200,
    error_category: null,
    error_message: null,
    ...overrides,
  };
}

export function adoAction(overrides: Partial<ActionDetail> = {}): ActionDetail {
  return {
    id: 'act-1',
    workflow_run_id: 'run-1',
    email_message_id: 'email-1',
    owner_user_id: 'u-1',
    action_type: 'create_azure_devops_work_item',
    target_integration: 'azure_devops',
    status: 'pending_approval',
    version: 3,
    payload: {
      kind: 'create_azure_devops_work_item',
      project: 'Payments',
      work_item_type: 'Bug',
      title: 'Checkout fails with 500 for EU customers',
      description: 'Customers in the EU region see HTTP 500 on checkout.',
      priority: 1,
      area_path: null,
      iteration_path: null,
      tags: ['checkout'],
      assigned_to: null,
      additional_fields: {},
      source_email: { email_id: 'email-1', subject: 'URGENT: checkout broken' },
    },
    payload_hash: HASH_V3,
    title: 'Create bug: Checkout fails with 500 for EU customers',
    rationale: 'The sender reports a production outage affecting checkout.',
    expected_impact: 'Engineering is notified with the reported facts.',
    required_permissions: ['azure_devops:vso.work_write', 'nuvexa:action.execute'],
    idempotency_key: 'f'.repeat(64),
    validation_errors: [],
    validation_warnings: [],
    source: 'planner',
    external_id: null,
    external_url: null,
    last_error: null,
    created_at: '2026-10-24T10:00:00Z',
    updated_at: '2026-10-24T10:00:00Z',
    executed_at: null,
    current_approval: null,
    permissions: {
      can_edit: true,
      can_approve: true,
      can_reject: true,
      can_cancel: true,
      can_execute: true,
      can_reconcile: false,
    },
    email_subject: 'URGENT: checkout broken',
    approvals: [],
    executions: [],
    ...overrides,
  };
}

export function replyAction(overrides: Partial<ActionDetail> = {}): ActionDetail {
  return adoAction({
    id: 'act-reply',
    action_type: 'propose_email_reply',
    target_integration: 'reply_drafts',
    title: 'Reply to Dana: acknowledge the outage',
    payload: {
      kind: 'propose_email_reply',
      to: ['dana@fabrikam.example'],
      subject: 'Re: URGENT: checkout broken',
      body: 'Hi Dana,\n\nThanks for reporting this. We are investigating.\n\nRegards',
      in_reply_to_internet_message_id: '<abc@fabrikam.example>',
      source_email: { email_id: 'email-1', subject: 'URGENT: checkout broken' },
    },
    required_permissions: ['nuvexa:action.execute'],
    ...overrides,
  });
}

export function followUpAction(overrides: Partial<ActionDetail> = {}): ActionDetail {
  return adoAction({
    id: 'act-follow',
    action_type: 'create_follow_up_task',
    target_integration: 'internal_tasks',
    title: 'Follow up with Dana on Friday',
    payload: {
      kind: 'create_follow_up_task',
      title: 'Call Dana back',
      description: 'Confirm the checkout fix with the customer.',
      due_at: '2026-11-02T09:30:00Z',
      source_email: { email_id: 'email-1' },
    },
    ...overrides,
  });
}

export function noActionAction(overrides: Partial<ActionDetail> = {}): ActionDetail {
  return adoAction({
    id: 'act-none',
    action_type: 'mark_no_action',
    target_integration: 'none',
    title: 'No action: newsletter',
    payload: {
      kind: 'mark_no_action',
      reason: 'Marketing newsletter, informational only.',
      source_email: { email_id: 'email-1' },
    },
    ...overrides,
  });
}

/** `ActionOut` (list shape) — the detail without approvals/executions. */
export function actionSummary(overrides: Partial<ActionOut> = {}): ActionOut {
  const { approvals: _a, executions: _e, ...rest } = adoAction();
  return { ...rest, ...overrides };
}

// --- Analysis / workflows ------------------------------------------------------------------------

export function analysis(overrides: Partial<AnalysisOut> = {}): AnalysisOut {
  return {
    id: 'an-1',
    workflow_run_id: 'run-1',
    attempt: 1,
    category: 'incident',
    priority: 'high',
    urgency: 'immediate',
    action_recommended: true,
    needs_more_info: false,
    confidence_score: 0.62,
    confidence_bucket: 'medium',
    confidence_method: 'heuristic-v1',
    classification: { rationale: 'Production checkout outage reported.' },
    extraction: {},
    injection_flags: [],
    validation_warnings: [],
    model_provider: 'heuristic',
    model_name: 'heuristic-demo',
    prompt_version: 'p1',
    schema_version: 's1',
    created_at: '2026-10-24T10:00:00Z',
    ...overrides,
  };
}

export function workflowRun(overrides: Partial<WorkflowRunOut> = {}): WorkflowRunOut {
  return {
    id: 'run-1',
    email_message_id: 'email-1',
    owner_user_id: 'u-1',
    status: 'pending_approval',
    current_step: 'await_review',
    trigger: 'manual',
    analysis_attempt: 1,
    correlation_id: 'corr-run-1',
    model_provider: 'heuristic',
    model_name: 'heuristic-demo',
    prompt_version: 'p1',
    schema_version: 's1',
    error_category: null,
    error_message: null,
    metrics: { total_tokens: 1234, total_latency_ms: 840 },
    awaiting_review_since: '2026-10-24T10:00:00Z',
    started_at: '2026-10-24T09:59:00Z',
    completed_at: null,
    created_at: '2026-10-24T09:59:00Z',
    updated_at: '2026-10-24T10:00:00Z',
    email_subject: 'URGENT: checkout broken',
    ...overrides,
  };
}

export function workflowEvent(overrides: Partial<WorkflowEventOut> = {}): WorkflowEventOut {
  return {
    id: 'ev-1',
    step: 'classify',
    event_type: 'llm_call',
    status: 'succeeded',
    message: null,
    latency_ms: 420,
    details: { model: 'heuristic-demo', usage: { input_tokens: 800, output_tokens: 120 } },
    created_at: '2026-10-24T09:59:10Z',
    ...overrides,
  };
}

export function workflowDetail(overrides: Partial<WorkflowDetail> = {}): WorkflowDetail {
  return {
    run: workflowRun(),
    analysis: analysis(),
    actions: [actionSummary()],
    events: [workflowEvent()],
    ...overrides,
  };
}

// --- Emails --------------------------------------------------------------------------------------

export function emailListItem(overrides: Partial<EmailListItem> = {}): EmailListItem {
  return {
    id: 'email-1',
    connected_account_id: 'conn-1',
    subject: 'URGENT: checkout broken',
    sender_name: 'Dana Customer',
    sender_address: 'dana@fabrikam.example',
    received_at: '2026-10-24T09:00:00Z',
    body_preview: 'Checkout returns 500 for EU customers…',
    has_attachments: false,
    is_read: false,
    analysis_status: 'awaiting_review',
    category: 'incident',
    priority: 'high',
    latest_workflow_run_id: 'run-1',
    content_changed_since_analysis: false,
    ...overrides,
  };
}

export function emailDetail(overrides: Partial<EmailDetail> = {}): EmailDetail {
  return {
    ...emailListItem(),
    internet_message_id: '<abc@fabrikam.example>',
    conversation_id: 'conv-1',
    to_recipients: [{ name: 'Support', address: 'support@contoso.example' }],
    cc_recipients: [],
    body_text: 'Hi team,\n\nCheckout returns HTTP 500 for EU customers since 08:40 UTC.\n\nDana',
    body_purged: false,
    attachments: [],
    web_link: 'https://outlook.office.com/mail/id/abc',
    importance: 'high',
    ...overrides,
  };
}

// --- Audit / tasks / drafts ----------------------------------------------------------------------

export function auditEvent(overrides: Partial<AuditEventOut> = {}): AuditEventOut {
  return {
    id: 'au-1',
    actor_user_id: 'u-approver',
    actor_name: 'Avery Approver',
    actor_type: 'user',
    event_type: 'action.approved',
    resource_type: 'action',
    resource_id: 'act-1',
    workflow_run_id: 'run-1',
    action_id: 'act-1',
    outcome: 'success',
    correlation_id: 'corr-audit-1',
    details: { version: 3 },
    created_at: '2026-10-24T10:05:00Z',
    ...overrides,
  };
}

export function taskFixture(overrides: Partial<FollowUpTaskOut> = {}): FollowUpTaskOut {
  return {
    id: 'task-1',
    title: 'Call Dana back',
    description: 'Confirm the fix with the customer.',
    due_at: '2026-10-20T09:00:00Z',
    status: 'open',
    action_id: 'act-follow',
    email_message_id: 'email-1',
    created_at: '2026-10-19T09:00:00Z',
    completed_at: null,
    ...overrides,
  };
}

export function replyDraftFixture(overrides: Partial<ReplyDraftOut> = {}): ReplyDraftOut {
  return {
    id: 'draft-1',
    to_recipients: ['dana@fabrikam.example'],
    subject: 'Re: URGENT: checkout broken',
    body: 'Hi Dana,\n\nWe deployed a fix at 10:30 UTC.\n\nRegards',
    action_id: 'act-reply',
    email_message_id: 'email-1',
    created_at: '2026-10-24T10:40:00Z',
    ...overrides,
  };
}

// --- Settings / integrations / dashboard ---------------------------------------------------------

export function settingsOut(overrides: Partial<SettingsOut> = {}): SettingsOut {
  return {
    version: 2,
    updated_at: '2026-10-24T08:00:00Z',
    settings: {
      azure_devops: {
        project: 'Payments',
        work_item_type: 'Task',
        area_path: null,
        iteration_path: null,
        allowed_projects: [],
        allowed_work_item_types: ['Bug', 'Task', 'User Story'],
        category_work_item_types: { incident: 'Bug', bug: 'Bug' },
        allowed_additional_fields: ['Microsoft.VSTS.Common.Severity'],
      },
      taxonomy: {
        enabled_categories: ['incident', 'bug', 'other'],
        category_descriptions: { incident: 'Production outage.' },
      },
      priority: {
        category_default_priority: { incident: 'high' },
        azure_devops_priority: { critical: 1, high: 1, medium: 2, low: 3 },
      },
      confidence: { high: 0.75, medium: 0.5 },
      retention: { email_body_days: 90, workflow_event_days: 180, audit_days: 365 },
      auto_analyze_new_emails: false,
      attachment_processing_enabled: false,
    },
    ...overrides,
  };
}

export function integrationsOut(overrides: Partial<IntegrationsOut> = {}): IntegrationsOut {
  return {
    microsoft: {
      mode: 'demo',
      entra_configured: false,
      required_scopes: ['Mail.Read', 'offline_access'],
      connections: [
        {
          id: 'conn-1',
          provider: 'demo_mailbox',
          status: 'active',
          email: 'demo@contoso.example',
          display_name: 'Demo mailbox',
          granted_scopes: ['Mail.Read'],
          connected_at: '2026-10-24T08:00:00Z',
          last_error: null,
          sync: {
            folder: 'Inbox',
            last_status: 'succeeded',
            last_sync_started_at: '2026-10-24T09:00:00Z',
            last_sync_completed_at: '2026-10-24T09:00:05Z',
            last_error: null,
            messages_ingested_total: 42,
          },
        },
      ],
    },
    azure_devops: {
      mode: 'mock',
      organization: 'contoso',
      configured: true,
      requires_user_connection: false,
      user_connected: false,
      connection: null,
      default_project: 'Payments',
      default_work_item_type: 'Task',
      api_version: '7.1',
    },
    llm: {
      provider: 'heuristic',
      model: 'heuristic-demo',
      structured_outputs: true,
      observability_enabled: false,
    },
    demo_mode: true,
    ...overrides,
  };
}

export function dashboardSummary(overrides: Partial<DashboardSummaryOut> = {}): DashboardSummaryOut {
  return {
    emails_total: 42,
    emails_analyzed: 30,
    actions_proposed: 25,
    pending_approvals: 4,
    completed_actions: 15,
    failed_actions: 1,
    uncertain_actions: 1,
    rejected_actions: 3,
    workflows_by_status: { pending_approval: 4, completed: 15, failed: 1 },
    categories: { incident: 6, bug: 9, information_only: 10 },
    avg_approval_wait_seconds: 5400,
    llm: { total_tokens: 125_000, estimated_cost_usd: 0.42, avg_latency_ms: 850 },
    recent_activity: [auditEvent()],
    ...overrides,
  };
}
