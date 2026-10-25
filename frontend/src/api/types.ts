/**
 * TypeScript mirror of the API contract.
 *
 * Sources of truth (keep in sync):
 *   app/domain/enums.py        — enum values
 *   app/schemas/api.py         — request/response models
 *   app/schemas/actions.py     — action payloads (per action_type)
 *   app/schemas/settings.py    — TenantSettings
 *   app/schemas/email.py       — AttachmentMeta, InjectionFlag
 *   app/integrations/azure_devops/base.py — ProjectInfo, WorkItemTypeInfo, ConnectionCheck, FieldIssue
 *
 * UUIDs and datetimes are serialized as strings (ISO-8601, UTC).
 */

// --- Enums ---------------------------------------------------------------------------------------

export const USER_ROLES = ['viewer', 'member', 'approver', 'admin'] as const;
export type UserRole = (typeof USER_ROLES)[number];

export type ConnectionProvider = 'microsoft_graph' | 'azure_devops' | 'demo_mailbox';
export type ConnectionStatus = 'active' | 'needs_reauth' | 'error' | 'disconnected';
export type SyncStatus = 'idle' | 'running' | 'succeeded' | 'failed';

export const EMAIL_ANALYSIS_STATUSES = [
  'not_analyzed',
  'queued',
  'analyzing',
  'awaiting_review',
  'done',
  'failed',
] as const;
export type EmailAnalysisStatus = (typeof EMAIL_ANALYSIS_STATUSES)[number];

export const EMAIL_CATEGORIES = [
  'incident',
  'bug',
  'service_request',
  'task_request',
  'feature_request',
  'follow_up',
  'information_only',
  'spam_or_irrelevant',
  'other',
] as const;
export type EmailCategory = (typeof EMAIL_CATEGORIES)[number];

export const URGENCIES = ['immediate', 'high', 'normal', 'low'] as const;
export type Urgency = (typeof URGENCIES)[number];

export const PRIORITIES = ['critical', 'high', 'medium', 'low'] as const;
export type Priority = (typeof PRIORITIES)[number];

export type ConfidenceBucket = 'high' | 'medium' | 'low';

export const WORKFLOW_STATUSES = [
  'received',
  'normalized',
  'classified',
  'extracted',
  'validated',
  'action_proposed',
  'pending_approval',
  'approved',
  'executing',
  'completed',
  'rejected',
  'failed',
  'cancelled',
] as const;
export type WorkflowStatus = (typeof WORKFLOW_STATUSES)[number];

export type WorkflowTrigger = 'manual' | 'auto';

export type WorkflowEventType =
  | 'step_started'
  | 'step_completed'
  | 'step_failed'
  | 'llm_call'
  | 'tool_call'
  | 'interrupt'
  | 'resume'
  | 'integration_call'
  | 'status_changed';

export const ACTION_TYPES = [
  'create_azure_devops_work_item',
  'propose_email_reply',
  'create_follow_up_task',
  'mark_no_action',
] as const;
export type ActionType = (typeof ACTION_TYPES)[number];

export type TargetIntegration = 'azure_devops' | 'reply_drafts' | 'internal_tasks' | 'none';

export const ACTION_STATUSES = [
  'pending_approval',
  'approved',
  'rejected',
  'cancelled',
  'executing',
  'completed',
  'failed',
  'uncertain',
  'superseded',
] as const;
export type ActionStatus = (typeof ACTION_STATUSES)[number];

export type ApprovalDecision = 'approved' | 'rejected';
export type ExecutionStatus = 'running' | 'succeeded' | 'failed' | 'uncertain';

export type FailureCategory =
  | 'validation'
  | 'authorization'
  | 'permission'
  | 'not_found'
  | 'rate_limited'
  | 'transient'
  | 'timeout'
  | 'ambiguous'
  | 'delta_expired'
  | 'configuration'
  | 'llm_output'
  | 'llm_provider'
  | 'internal';

export const TASK_STATUSES = ['open', 'done', 'dismissed'] as const;
export type TaskStatus = (typeof TASK_STATUSES)[number];

export type ActorType = 'user' | 'system' | 'agent';

export const AUDIT_OUTCOMES = ['success', 'failure', 'denied'] as const;
export type AuditOutcome = (typeof AUDIT_OUTCOMES)[number];

/** Permission strings returned in `MeOut.permissions`. */
export type Permission =
  | 'email:read'
  | 'email:read_all'
  | 'email:sync'
  | 'workflow:run'
  | 'action:edit'
  | 'action:approve'
  | 'action:execute'
  | 'action:approve_any'
  | 'audit:read_all'
  | 'settings:write';

// --- Errors / pagination -------------------------------------------------------------------------

export interface ErrorBody {
  code: string;
  message: string;
  correlation_id: string | null;
  details: Record<string, unknown>[];
}

export interface ErrorResponse {
  error: ErrorBody;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface JobAccepted {
  job_ids: string[];
  message: string;
}

// --- Auth ----------------------------------------------------------------------------------------

export interface AuthConfigOut {
  app_name: string;
  environment: string;
  demo_mode: boolean;
  demo_login_enabled: boolean;
  entra_login_enabled: boolean;
}

export interface UserOut {
  id: string;
  email: string;
  display_name: string;
  role: UserRole;
  tenant_id: string;
  tenant_name: string;
}

export interface MeOut {
  user: UserOut;
  permissions: string[];
}

export type DemoPersona = 'admin' | 'approver' | 'member' | 'viewer';

export interface DemoLoginRequest {
  persona: DemoPersona;
}

// --- Integrations --------------------------------------------------------------------------------

export interface SyncStateOut {
  folder: string;
  last_status: SyncStatus;
  last_sync_started_at: string | null;
  last_sync_completed_at: string | null;
  last_error: string | null;
  messages_ingested_total: number;
}

export interface ConnectionOut {
  id: string;
  provider: ConnectionProvider;
  status: ConnectionStatus;
  email: string | null;
  display_name: string | null;
  granted_scopes: string[];
  connected_at: string | null;
  last_error: string | null;
  sync: SyncStateOut | null;
}

export interface MicrosoftIntegrationOut {
  mode: string;
  entra_configured: boolean;
  required_scopes: string[];
  connections: ConnectionOut[];
}

export interface AzureDevOpsIntegrationOut {
  mode: string;
  organization: string | null;
  configured: boolean;
  requires_user_connection: boolean;
  user_connected: boolean;
  /** The caller's own delegated (entra_delegated) connection; null when not connected. */
  connection: ConnectionOut | null;
  default_project: string | null;
  default_work_item_type: string;
  api_version: string;
}

export interface LLMIntegrationOut {
  provider: string;
  model: string;
  structured_outputs: boolean;
  observability_enabled: boolean;
}

export interface IntegrationsOut {
  microsoft: MicrosoftIntegrationOut;
  azure_devops: AzureDevOpsIntegrationOut;
  llm: LLMIntegrationOut;
  demo_mode: boolean;
}

export interface SyncRequest {
  connected_account_id?: string | null;
}

// --- Emails --------------------------------------------------------------------------------------

export interface AttachmentMeta {
  name: string;
  content_type: string | null;
  size: number | null;
  is_inline: boolean;
}

export interface InjectionFlag {
  rule: string;
  severity: 'low' | 'medium' | 'high';
  excerpt: string;
}

export interface EmailListItem {
  id: string;
  connected_account_id: string;
  subject: string;
  sender_name: string | null;
  sender_address: string | null;
  received_at: string;
  body_preview: string;
  has_attachments: boolean;
  is_read: boolean;
  analysis_status: EmailAnalysisStatus;
  category: EmailCategory | null;
  priority: Priority | null;
  latest_workflow_run_id: string | null;
  content_changed_since_analysis: boolean;
}

export interface RecipientOut {
  name: string | null;
  address: string | null;
}

export interface EmailDetail extends EmailListItem {
  internet_message_id: string | null;
  conversation_id: string | null;
  to_recipients: RecipientOut[];
  cc_recipients: RecipientOut[];
  body_text: string | null;
  body_purged: boolean;
  attachments: AttachmentMeta[];
  web_link: string | null;
  importance: string | null;
}

export interface EmailListFilters {
  q?: string;
  category?: EmailCategory;
  priority?: Priority;
  analysis_status?: EmailAnalysisStatus;
  connected_account_id?: string;
  limit?: number;
  offset?: number;
}

// --- Workflows -----------------------------------------------------------------------------------

export interface WorkflowRunOut {
  id: string;
  email_message_id: string;
  owner_user_id: string;
  status: WorkflowStatus;
  current_step: string | null;
  trigger: WorkflowTrigger;
  analysis_attempt: number;
  correlation_id: string | null;
  model_provider: string | null;
  model_name: string | null;
  prompt_version: string | null;
  schema_version: string | null;
  error_category: FailureCategory | null;
  error_message: string | null;
  metrics: Record<string, unknown>;
  awaiting_review_since: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  email_subject: string | null;
}

export interface StartAnalysisOut {
  workflow_run: WorkflowRunOut;
  created: boolean;
  job_id: string | null;
}

/**
 * `AnalysisOut.classification` is the serialized `ClassificationOutput` and `extraction` the
 * serialized `ValidatedExtraction` (app/schemas/ai.py). They are typed as open dictionaries in the
 * API, so every field is treated as optional and read defensively.
 */
export interface ClassificationData {
  category?: EmailCategory;
  rationale?: string;
  urgency?: Urgency;
  priority?: Priority;
  action_recommended?: boolean;
  needs_more_info?: boolean;
  missing_information?: string[];
  extraction_warnings?: string[];
  confidence?: number;
}

export interface EvidenceSnippet {
  field: string;
  quote: string;
}

export interface ProposedField {
  name: string;
  value: string;
}

export interface ExtractionData {
  summary?: string;
  description?: string;
  requester_name?: string | null;
  requester_email?: string | null;
  organization?: string | null;
  product_or_system?: string | null;
  environment?: string | null;
  impact?: string | null;
  deadline?: string | null;
  suggested_owner?: string | null;
  suggested_team?: string | null;
  reference_urls?: string[];
  reference_ids?: string[];
  references?: string[];
  suggested_work_item_type?: string | null;
  proposed_fields?: ProposedField[];
  evidence?: EvidenceSnippet[];
}

export interface AnalysisOut {
  id: string;
  workflow_run_id: string;
  attempt: number;
  category: EmailCategory;
  priority: Priority;
  urgency: Urgency;
  action_recommended: boolean;
  needs_more_info: boolean;
  confidence_score: number;
  confidence_bucket: ConfidenceBucket;
  confidence_method: string;
  classification: ClassificationData & Record<string, unknown>;
  extraction: ExtractionData & Record<string, unknown>;
  injection_flags: InjectionFlag[];
  validation_warnings: string[];
  model_provider: string;
  model_name: string;
  prompt_version: string;
  schema_version: string;
  created_at: string;
}

export interface WorkflowEventOut {
  id: string;
  step: string;
  event_type: WorkflowEventType;
  status: string;
  message: string | null;
  latency_ms: number | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface WorkflowDetail {
  run: WorkflowRunOut;
  analysis: AnalysisOut | null;
  actions: ActionOut[];
  events: WorkflowEventOut[];
}

// --- Actions -------------------------------------------------------------------------------------

export interface ApprovalOut {
  id: string;
  decision: ApprovalDecision;
  decided_by_user_id: string;
  decided_by_name: string | null;
  action_version: number;
  payload_hash: string;
  reason: string | null;
  created_at: string;
  invalidated_at: string | null;
  invalidated_reason: string | null;
}

export interface ExecutionOut {
  id: string;
  attempt_number: number;
  status: ExecutionStatus;
  deduplicated: boolean;
  requested_by_user_id: string;
  action_version: number;
  started_at: string;
  finished_at: string | null;
  latency_ms: number | null;
  retry_count: number;
  external_id: string | null;
  external_url: string | null;
  provider_status_code: number | null;
  error_category: FailureCategory | null;
  error_message: string | null;
}

export interface ActionPermissions {
  can_edit: boolean;
  can_approve: boolean;
  can_reject: boolean;
  can_cancel: boolean;
  can_execute: boolean;
  can_reconcile: boolean;
}

export interface ActionOut {
  id: string;
  workflow_run_id: string;
  email_message_id: string;
  owner_user_id: string;
  action_type: ActionType;
  target_integration: TargetIntegration;
  status: ActionStatus;
  version: number;
  payload: Record<string, unknown>;
  payload_hash: string;
  title: string;
  rationale: string;
  expected_impact: string;
  required_permissions: string[];
  idempotency_key: string;
  validation_errors: string[];
  validation_warnings: string[];
  source: string;
  external_id: string | null;
  external_url: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  executed_at: string | null;
  current_approval: ApprovalOut | null;
  permissions: ActionPermissions | null;
  email_subject: string | null;
}

export interface ActionDetail extends ActionOut {
  approvals: ApprovalOut[];
  executions: ExecutionOut[];
}

export interface ActionUpdateRequest {
  expected_version: number;
  payload: ActionPayload | Record<string, unknown>;
}

export interface ApproveRequest {
  version: number;
  payload_hash: string;
  comment?: string | null;
}

export interface RejectRequest {
  reason: string;
}

export interface CancelRequest {
  reason?: string | null;
}

export interface ExecuteRequest {
  version: number;
  payload_hash: string;
}

export interface ExecuteAccepted {
  action: ActionOut;
  job_id: string;
}

// --- Action payloads (app/schemas/actions.py) ----------------------------------------------------

export interface SourceEmailReference {
  email_id: string;
  internet_message_id?: string | null;
  subject?: string;
  sender?: string | null;
  received_at?: string | null;
}

export interface AzureDevOpsWorkItemPayload {
  kind: 'create_azure_devops_work_item';
  project: string;
  work_item_type: string;
  title: string;
  description: string;
  priority: number;
  area_path: string | null;
  iteration_path: string | null;
  tags: string[];
  assigned_to: string | null;
  additional_fields: Record<string, string | number>;
  source_email: SourceEmailReference;
}

export interface EmailReplyDraftPayload {
  kind: 'propose_email_reply';
  to: string[];
  subject: string;
  body: string;
  in_reply_to_internet_message_id: string | null;
  source_email: SourceEmailReference;
}

export interface FollowUpTaskPayload {
  kind: 'create_follow_up_task';
  title: string;
  description: string;
  due_at: string | null;
  source_email: SourceEmailReference;
}

export interface NoActionPayload {
  kind: 'mark_no_action';
  reason: string;
  source_email: SourceEmailReference;
}

export type ActionPayload =
  AzureDevOpsWorkItemPayload | EmailReplyDraftPayload | FollowUpTaskPayload | NoActionPayload;

// --- Azure DevOps --------------------------------------------------------------------------------

export interface ProjectInfo {
  id: string;
  name: string;
  state: string | null;
  url: string | null;
}

export interface WorkItemTypeInfo {
  name: string;
  reference_name: string | null;
  description: string | null;
  is_disabled: boolean;
}

export interface AdoConnectionTestOut {
  ok: boolean;
  mode: string;
  organization: string | null;
  message: string;
  latency_ms: number | null;
}

export interface AdoProjectsOut {
  items: ProjectInfo[];
}

export interface AdoWorkItemTypesOut {
  project: string;
  items: WorkItemTypeInfo[];
}

export interface FieldIssue {
  field: string | null;
  message: string;
}

export interface AdoValidateRequest {
  payload: Record<string, unknown>;
  server_side: boolean;
}

export interface AdoValidateOut {
  valid: boolean;
  schema_issues: FieldIssue[];
  policy_issues: string[];
  server_issues: FieldIssue[];
  server_validated: boolean;
}

// --- Audit / tasks / drafts / settings / dashboard ----------------------------------------------

export interface AuditEventOut {
  id: string;
  actor_user_id: string | null;
  actor_name: string | null;
  actor_type: ActorType;
  event_type: string;
  resource_type: string;
  resource_id: string | null;
  workflow_run_id: string | null;
  action_id: string | null;
  outcome: AuditOutcome;
  correlation_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface FollowUpTaskOut {
  id: string;
  title: string;
  description: string;
  due_at: string | null;
  status: TaskStatus;
  action_id: string | null;
  email_message_id: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface TaskUpdateRequest {
  status: TaskStatus;
}

export interface ReplyDraftOut {
  id: string;
  to_recipients: string[];
  subject: string;
  body: string;
  action_id: string | null;
  email_message_id: string | null;
  created_at: string;
}

export interface AzureDevOpsDefaults {
  project: string | null;
  work_item_type: string;
  area_path: string | null;
  iteration_path: string | null;
  allowed_projects: string[];
  allowed_work_item_types: string[];
  category_work_item_types: Partial<Record<EmailCategory, string>>;
  allowed_additional_fields: string[];
}

export interface TaxonomyConfig {
  enabled_categories: EmailCategory[];
  category_descriptions: Partial<Record<EmailCategory, string>>;
}

export interface PriorityMapping {
  category_default_priority: Partial<Record<EmailCategory, Priority>>;
  azure_devops_priority: Record<Priority, number>;
}

export interface ConfidenceThresholds {
  high: number;
  medium: number;
}

export interface RetentionConfig {
  email_body_days: number;
  workflow_event_days: number;
  audit_days: number;
}

export interface TenantSettings {
  azure_devops: AzureDevOpsDefaults;
  taxonomy: TaxonomyConfig;
  priority: PriorityMapping;
  confidence: ConfidenceThresholds;
  retention: RetentionConfig;
  auto_analyze_new_emails: boolean;
  attachment_processing_enabled: boolean;
}

export interface SettingsOut {
  settings: TenantSettings;
  version: number;
  updated_at: string | null;
}

export interface SettingsUpdateRequest {
  expected_version: number;
  settings: TenantSettings;
}

export interface LLMUsageSummary {
  total_tokens: number;
  estimated_cost_usd: number | null;
  avg_latency_ms: number | null;
}

export interface DashboardSummaryOut {
  emails_total: number;
  emails_analyzed: number;
  actions_proposed: number;
  pending_approvals: number;
  completed_actions: number;
  failed_actions: number;
  uncertain_actions: number;
  rejected_actions: number;
  workflows_by_status: Record<string, number>;
  categories: Record<string, number>;
  avg_approval_wait_seconds: number | null;
  llm: LLMUsageSummary;
  recent_activity: AuditEventOut[];
}

export interface HealthOut {
  status: 'ok' | 'degraded' | 'error';
  version: string;
  checks: Record<string, string>;
}
