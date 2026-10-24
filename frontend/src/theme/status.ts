/**
 * Single source of truth for status presentation. Colour is never the only signal: every status
 * also has a text label and an icon, and in-flight statuses show a loader.
 *
 * Palette contract:
 *   pending → orange · approved → blue · running/executing → cyan + loader · completed → green
 *   failed → red · uncertain → grape · rejected → red outline · cancelled → gray · superseded → dimmed
 */
import type {
  ActionStatus,
  AuditOutcome,
  ConnectionStatus,
  EmailAnalysisStatus,
  ExecutionStatus,
  SyncStatus,
  TaskStatus,
  WorkflowStatus,
} from '../api/types';

export type StatusTone =
  | 'pending'
  | 'approved'
  | 'running'
  | 'completed'
  | 'failed'
  | 'uncertain'
  | 'rejected'
  | 'cancelled'
  | 'superseded'
  | 'neutral'
  | 'warning';

export type StatusIconName =
  | 'clock'
  | 'check'
  | 'loader'
  | 'circle-check'
  | 'alert'
  | 'help'
  | 'x'
  | 'ban'
  | 'layers-off'
  | 'circle'
  | 'inbox'
  | 'plug'
  | 'plug-off';

export interface StatusDescriptor {
  label: string;
  tone: StatusTone;
  color: string;
  variant: 'light' | 'filled' | 'outline' | 'default';
  icon: StatusIconName;
  /** In-flight: show an animated loader instead of the static icon. */
  loading?: boolean;
  /** Render visually de-emphasised (superseded proposals). */
  dimmed?: boolean;
  /** Short explanation shown in a tooltip / to screen readers. */
  description?: string;
}

const TONES: Record<StatusTone, Pick<StatusDescriptor, 'color' | 'variant' | 'dimmed'>> = {
  pending: { color: 'orange', variant: 'light' },
  approved: { color: 'blue', variant: 'light' },
  running: { color: 'cyan', variant: 'light' },
  completed: { color: 'green', variant: 'light' },
  failed: { color: 'red', variant: 'light' },
  uncertain: { color: 'grape', variant: 'light' },
  rejected: { color: 'red', variant: 'outline' },
  cancelled: { color: 'gray', variant: 'light' },
  superseded: { color: 'gray', variant: 'default', dimmed: true },
  neutral: { color: 'gray', variant: 'outline' },
  warning: { color: 'yellow', variant: 'light' },
};

function d(
  label: string,
  tone: StatusTone,
  icon: StatusIconName,
  extra: Partial<StatusDescriptor> = {},
): StatusDescriptor {
  return { label, tone, icon, ...TONES[tone], ...extra };
}

export const ACTION_STATUS: Record<ActionStatus, StatusDescriptor> = {
  pending_approval: d('Pending approval', 'pending', 'clock', {
    description: 'Waiting for a human to review and approve the exact payload.',
  }),
  approved: d('Approved', 'approved', 'check', {
    description: 'Approved for this version and payload hash. Not yet executed.',
  }),
  executing: d('Executing', 'running', 'loader', {
    loading: true,
    description: 'The approved payload is being written to the target system.',
  }),
  completed: d('Completed', 'completed', 'circle-check', { description: 'Executed successfully.' }),
  failed: d('Failed', 'failed', 'alert', {
    description: 'Execution was confirmed not to have created anything. It can be retried.',
  }),
  uncertain: d('Uncertain', 'uncertain', 'help', {
    description: 'The outcome of the external write is unknown. Reconcile before retrying.',
  }),
  rejected: d('Rejected', 'rejected', 'x', { description: 'A reviewer rejected this proposal.' }),
  cancelled: d('Cancelled', 'cancelled', 'ban', { description: 'The proposal was cancelled.' }),
  superseded: d('Superseded', 'superseded', 'layers-off', {
    description: 'Replaced by a newer analysis of the same email.',
  }),
};

export const WORKFLOW_STATUS: Record<WorkflowStatus, StatusDescriptor> = {
  received: d('Received', 'running', 'loader', { loading: true }),
  normalized: d('Normalized', 'running', 'loader', { loading: true }),
  classified: d('Classified', 'running', 'loader', { loading: true }),
  extracted: d('Extracted', 'running', 'loader', { loading: true }),
  validated: d('Validated', 'running', 'loader', { loading: true }),
  action_proposed: d('Action proposed', 'running', 'loader', { loading: true }),
  pending_approval: d('Pending approval', 'pending', 'clock'),
  approved: d('Approved', 'approved', 'check'),
  executing: d('Executing', 'running', 'loader', { loading: true }),
  completed: d('Completed', 'completed', 'circle-check'),
  rejected: d('Rejected', 'rejected', 'x'),
  failed: d('Failed', 'failed', 'alert'),
  cancelled: d('Cancelled', 'cancelled', 'ban'),
};

export const EMAIL_ANALYSIS_STATUS: Record<EmailAnalysisStatus, StatusDescriptor> = {
  not_analyzed: d('Not analyzed', 'neutral', 'circle'),
  queued: d('Queued', 'running', 'clock'),
  analyzing: d('Analyzing', 'running', 'loader', { loading: true }),
  awaiting_review: d('Awaiting review', 'pending', 'clock'),
  done: d('Done', 'completed', 'circle-check'),
  failed: d('Failed', 'failed', 'alert'),
};

export const EXECUTION_STATUS: Record<ExecutionStatus, StatusDescriptor> = {
  running: d('Running', 'running', 'loader', { loading: true }),
  succeeded: d('Succeeded', 'completed', 'circle-check'),
  failed: d('Failed', 'failed', 'alert'),
  uncertain: d('Uncertain', 'uncertain', 'help'),
};

export const CONNECTION_STATUS: Record<ConnectionStatus, StatusDescriptor> = {
  active: d('Active', 'completed', 'plug'),
  needs_reauth: d('Needs re-authentication', 'pending', 'alert'),
  error: d('Error', 'failed', 'alert'),
  disconnected: d('Disconnected', 'cancelled', 'plug-off'),
};

export const SYNC_STATUS: Record<SyncStatus, StatusDescriptor> = {
  idle: d('Idle', 'neutral', 'circle'),
  running: d('Syncing', 'running', 'loader', { loading: true }),
  succeeded: d('Synced', 'completed', 'circle-check'),
  failed: d('Sync failed', 'failed', 'alert'),
};

export const TASK_STATUS: Record<TaskStatus, StatusDescriptor> = {
  open: d('Open', 'pending', 'clock'),
  done: d('Done', 'completed', 'circle-check'),
  dismissed: d('Dismissed', 'cancelled', 'ban'),
};

export const AUDIT_OUTCOME: Record<AuditOutcome, StatusDescriptor> = {
  success: d('Success', 'completed', 'circle-check'),
  failure: d('Failure', 'failed', 'alert'),
  denied: d('Denied', 'rejected', 'ban'),
};

export const STATUS_MAPS = {
  action: ACTION_STATUS,
  workflow: WORKFLOW_STATUS,
  email: EMAIL_ANALYSIS_STATUS,
  execution: EXECUTION_STATUS,
  connection: CONNECTION_STATUS,
  sync: SYNC_STATUS,
  task: TASK_STATUS,
  audit: AUDIT_OUTCOME,
} as const;

export type StatusKind = keyof typeof STATUS_MAPS;

function humanize(value: string): string {
  const text = value.replace(/[_.-]+/g, ' ').trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : value;
}

/** Look up a descriptor; unknown values (forward-compatible API additions) render neutrally. */
export function getStatusDescriptor(kind: StatusKind, status: string): StatusDescriptor {
  const map = STATUS_MAPS[kind] as Record<string, StatusDescriptor>;
  return map[status] ?? d(humanize(status), 'neutral', 'circle');
}

// --- In-flight detection (drives polling) ------------------------------------------------------

export const IN_FLIGHT_WORKFLOW_STATUSES: ReadonlySet<WorkflowStatus> = new Set<WorkflowStatus>([
  'received',
  'normalized',
  'classified',
  'extracted',
  'validated',
  'action_proposed',
  'executing',
]);

export const TERMINAL_WORKFLOW_STATUSES: ReadonlySet<WorkflowStatus> = new Set<WorkflowStatus>([
  'completed',
  'rejected',
  'cancelled',
]);

export function isWorkflowInFlight(status: WorkflowStatus | null | undefined): boolean {
  return status ? IN_FLIGHT_WORKFLOW_STATUSES.has(status) : false;
}

export function isActionInFlight(status: ActionStatus | null | undefined): boolean {
  return status === 'executing';
}

export function isEmailInFlight(status: EmailAnalysisStatus | null | undefined): boolean {
  return status === 'queued' || status === 'analyzing';
}

/** Polling cadence while something is in flight. */
export const POLL_INTERVAL_MS = 2000;
