/**
 * Contract test: every enum the dashboard knows about must match app/domain/enums.py exactly.
 *
 * Two layers:
 *  1. A hard-coded snapshot of the backend enums (always runs, also in a frontend-only checkout).
 *  2. When the backend source is present, the snapshot is verified against the live Python file, so
 *     a backend change fails here instead of silently rendering "unknown" statuses.
 *
 * Union-only types are pinned with `Record<Union, true>` objects: TypeScript rejects missing or
 * extra members, so the runtime key list is guaranteed to equal the type.
 */
import { describe, expect, it } from 'vitest';

import {
  CATEGORY_LABELS,
  PRIORITY_LABELS,
  TARGET_LABELS,
  URGENCY_LABELS,
  ACTION_TYPE_LABELS,
} from '../lib/format';
import {
  ACTION_STATUS,
  AUDIT_OUTCOME,
  CONNECTION_STATUS,
  EMAIL_ANALYSIS_STATUS,
  EXECUTION_STATUS,
  SYNC_STATUS,
  TASK_STATUS,
  TERMINAL_WORKFLOW_STATUSES,
  WORKFLOW_STATUS,
} from '../theme/status';
import {
  ACTION_STATUSES,
  ACTION_TYPES,
  AUDIT_OUTCOMES,
  EMAIL_ANALYSIS_STATUSES,
  EMAIL_CATEGORIES,
  PRIORITIES,
  TASK_STATUSES,
  URGENCIES,
  USER_ROLES,
  WORKFLOW_STATUSES,
  type ActorType,
  type ApprovalDecision,
  type ConfidenceBucket,
  type ConnectionProvider,
  type DemoPersona,
  type FailureCategory,
  type Permission,
  type WorkflowEventType,
  type WorkflowTrigger,
} from './types';

// --- Backend snapshot (app/domain/enums.py) ------------------------------------------------------

const BACKEND_ENUMS = {
  UserRole: ['viewer', 'member', 'approver', 'admin'],
  ConnectionProvider: ['microsoft_graph', 'azure_devops', 'demo_mailbox'],
  ConnectionStatus: ['active', 'needs_reauth', 'error', 'disconnected'],
  SyncStatus: ['idle', 'running', 'succeeded', 'failed'],
  EmailAnalysisStatus: ['not_analyzed', 'queued', 'analyzing', 'awaiting_review', 'done', 'failed'],
  EmailCategory: [
    'incident',
    'bug',
    'service_request',
    'task_request',
    'feature_request',
    'follow_up',
    'information_only',
    'spam_or_irrelevant',
    'other',
  ],
  Urgency: ['immediate', 'high', 'normal', 'low'],
  Priority: ['critical', 'high', 'medium', 'low'],
  ConfidenceBucket: ['high', 'medium', 'low'],
  WorkflowStatus: [
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
  ],
  WorkflowTrigger: ['manual', 'auto'],
  WorkflowEventType: [
    'step_started',
    'step_completed',
    'step_failed',
    'llm_call',
    'tool_call',
    'interrupt',
    'resume',
    'integration_call',
    'status_changed',
  ],
  ActionType: [
    'create_azure_devops_work_item',
    'propose_email_reply',
    'create_follow_up_task',
    'mark_no_action',
  ],
  TargetIntegration: ['azure_devops', 'reply_drafts', 'internal_tasks', 'none'],
  ActionStatus: [
    'pending_approval',
    'approved',
    'rejected',
    'cancelled',
    'executing',
    'completed',
    'failed',
    'uncertain',
    'superseded',
  ],
  ApprovalDecision: ['approved', 'rejected'],
  ExecutionStatus: ['running', 'succeeded', 'failed', 'uncertain'],
  FailureCategory: [
    'validation',
    'authorization',
    'permission',
    'not_found',
    'rate_limited',
    'transient',
    'timeout',
    'ambiguous',
    'delta_expired',
    'configuration',
    'llm_output',
    'llm_provider',
    'internal',
  ],
  TaskStatus: ['open', 'done', 'dismissed'],
  ActorType: ['user', 'system', 'agent'],
  AuditOutcome: ['success', 'failure', 'denied'],
} as const satisfies Record<string, readonly string[]>;

type BackendEnum = keyof typeof BACKEND_ENUMS;

const BACKEND_TERMINAL_WORKFLOW_STATUSES = ['completed', 'rejected', 'cancelled'];

/** app/services/authz.py `class Perm`. */
const BACKEND_PERMISSIONS = [
  'email:read',
  'email:read_all',
  'email:sync',
  'workflow:run',
  'action:edit',
  'action:approve',
  'action:execute',
  'action:approve_any',
  'audit:read_all',
  'settings:write',
];

// --- Frontend union types pinned as exhaustive records ------------------------------------------

const CONNECTION_PROVIDERS: Record<ConnectionProvider, true> = {
  microsoft_graph: true,
  azure_devops: true,
  demo_mailbox: true,
};
const CONFIDENCE_BUCKETS: Record<ConfidenceBucket, true> = { high: true, medium: true, low: true };
const WORKFLOW_TRIGGERS: Record<WorkflowTrigger, true> = { manual: true, auto: true };
const WORKFLOW_EVENT_TYPES: Record<WorkflowEventType, true> = {
  step_started: true,
  step_completed: true,
  step_failed: true,
  llm_call: true,
  tool_call: true,
  interrupt: true,
  resume: true,
  integration_call: true,
  status_changed: true,
};
const APPROVAL_DECISIONS: Record<ApprovalDecision, true> = { approved: true, rejected: true };
const FAILURE_CATEGORIES: Record<FailureCategory, true> = {
  validation: true,
  authorization: true,
  permission: true,
  not_found: true,
  rate_limited: true,
  transient: true,
  timeout: true,
  ambiguous: true,
  delta_expired: true,
  configuration: true,
  llm_output: true,
  llm_provider: true,
  internal: true,
};
const ACTOR_TYPES: Record<ActorType, true> = { user: true, system: true, agent: true };
const DEMO_PERSONAS: Record<DemoPersona, true> = { admin: true, approver: true, member: true, viewer: true };
const PERMISSIONS: Record<Permission, true> = {
  'email:read': true,
  'email:read_all': true,
  'email:sync': true,
  'workflow:run': true,
  'action:edit': true,
  'action:approve': true,
  'action:execute': true,
  'action:approve_any': true,
  'audit:read_all': true,
  'settings:write': true,
};

const keys = (record: object) => Object.keys(record);
const sorted = (values: readonly string[]) => [...values].sort();

/** Ordered const arrays (order matters: filters/steppers render in this order). */
const ORDERED: [string, BackendEnum, readonly string[]][] = [
  ['USER_ROLES', 'UserRole', USER_ROLES],
  ['EMAIL_ANALYSIS_STATUSES', 'EmailAnalysisStatus', EMAIL_ANALYSIS_STATUSES],
  ['EMAIL_CATEGORIES', 'EmailCategory', EMAIL_CATEGORIES],
  ['URGENCIES', 'Urgency', URGENCIES],
  ['PRIORITIES', 'Priority', PRIORITIES],
  ['WORKFLOW_STATUSES', 'WorkflowStatus', WORKFLOW_STATUSES],
  ['ACTION_TYPES', 'ActionType', ACTION_TYPES],
  ['ACTION_STATUSES', 'ActionStatus', ACTION_STATUSES],
  ['TASK_STATUSES', 'TaskStatus', TASK_STATUSES],
  ['AUDIT_OUTCOMES', 'AuditOutcome', AUDIT_OUTCOMES],
];

/** Lookup tables keyed by enum value (order irrelevant, coverage must be exact). */
const KEYED: [string, BackendEnum, string[]][] = [
  ['DemoPersona', 'UserRole', keys(DEMO_PERSONAS)],
  ['ConnectionProvider', 'ConnectionProvider', keys(CONNECTION_PROVIDERS)],
  ['CONNECTION_STATUS (status.ts)', 'ConnectionStatus', keys(CONNECTION_STATUS)],
  ['SYNC_STATUS (status.ts)', 'SyncStatus', keys(SYNC_STATUS)],
  ['EMAIL_ANALYSIS_STATUS (status.ts)', 'EmailAnalysisStatus', keys(EMAIL_ANALYSIS_STATUS)],
  ['CATEGORY_LABELS (format.ts)', 'EmailCategory', keys(CATEGORY_LABELS)],
  ['URGENCY_LABELS (format.ts)', 'Urgency', keys(URGENCY_LABELS)],
  ['PRIORITY_LABELS (format.ts)', 'Priority', keys(PRIORITY_LABELS)],
  ['ConfidenceBucket', 'ConfidenceBucket', keys(CONFIDENCE_BUCKETS)],
  ['WORKFLOW_STATUS (status.ts)', 'WorkflowStatus', keys(WORKFLOW_STATUS)],
  ['WorkflowTrigger', 'WorkflowTrigger', keys(WORKFLOW_TRIGGERS)],
  ['WorkflowEventType', 'WorkflowEventType', keys(WORKFLOW_EVENT_TYPES)],
  ['ACTION_TYPE_LABELS (format.ts)', 'ActionType', keys(ACTION_TYPE_LABELS)],
  ['TARGET_LABELS (format.ts)', 'TargetIntegration', keys(TARGET_LABELS)],
  ['ACTION_STATUS (status.ts)', 'ActionStatus', keys(ACTION_STATUS)],
  ['ApprovalDecision', 'ApprovalDecision', keys(APPROVAL_DECISIONS)],
  ['EXECUTION_STATUS (status.ts)', 'ExecutionStatus', keys(EXECUTION_STATUS)],
  ['FailureCategory', 'FailureCategory', keys(FAILURE_CATEGORIES)],
  ['TASK_STATUS (status.ts)', 'TaskStatus', keys(TASK_STATUS)],
  ['ActorType', 'ActorType', keys(ACTOR_TYPES)],
  ['AUDIT_OUTCOME (status.ts)', 'AuditOutcome', keys(AUDIT_OUTCOME)],
];

describe('frontend enums match the backend contract (snapshot of app/domain/enums.py)', () => {
  it.each(ORDERED)('%s equals %s in value and order', (_name, backend, values) => {
    expect([...values]).toEqual([...BACKEND_ENUMS[backend]]);
  });

  it.each(KEYED)('%s covers exactly the %s values', (_name, backend, values) => {
    expect(sorted(values)).toEqual(sorted(BACKEND_ENUMS[backend]));
  });

  it('TERMINAL_WORKFLOW_STATUSES equals the backend terminal set', () => {
    expect(sorted([...TERMINAL_WORKFLOW_STATUSES])).toEqual(sorted(BACKEND_TERMINAL_WORKFLOW_STATUSES));
  });

  it('Permission covers exactly the backend Perm strings (app/services/authz.py)', () => {
    expect(sorted(keys(PERMISSIONS))).toEqual(sorted(BACKEND_PERMISSIONS));
  });

  it('FailureCategory includes delta_expired (regression: was missing from the union)', () => {
    expect(keys(FAILURE_CATEGORIES)).toContain('delta_expired');
  });
});

// --- Live comparison against the Python sources ------------------------------------------------

/**
 * Read a backend source file relative to this test, or `undefined` when it is not checked out.
 * Uses `process.getBuiltinModule` (Node ≥ 22.3) so the browser-typed app program needs no Node
 * typings; Vite's `import.meta.glob` cannot be used because files outside the frontend root are
 * denied by the dev-server fs allow-list.
 */
function readBackendSource(relativePath: string): string | undefined {
  interface MinimalFs {
    existsSync(path: URL): boolean;
    readFileSync(path: URL, encoding: 'utf8'): string;
  }
  const proc = (globalThis as { process?: { getBuiltinModule?: (id: string) => unknown } }).process;
  const fs = proc?.getBuiltinModule?.('node:fs') as MinimalFs | undefined;
  const url = new URL(relativePath, import.meta.url);
  if (!fs || url.protocol !== 'file:' || !fs.existsSync(url)) return undefined;
  return fs.readFileSync(url, 'utf8');
}

const enumsSource = readBackendSource('../../../app/domain/enums.py');
const authzSource = readBackendSource('../../../app/services/authz.py');

/** Parse `class Name(StrEnum):` blocks into {Name: [values…]} (declaration order). */
function parseStrEnums(source: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  let current: string[] | null = null;
  for (const line of source.split('\n')) {
    const header = /^class (\w+)\(StrEnum\):/.exec(line);
    if (header) {
      current = [];
      out[header[1]!] = current;
      continue;
    }
    if (current && /^\S/.test(line)) current = null;
    const member = current ? /^\s+[A-Z][A-Z0-9_]* = "([^"]*)"/.exec(line) : null;
    if (member && current) current.push(member[1]!);
  }
  return out;
}

/** `TERMINAL_WORKFLOW_STATUSES = frozenset({WorkflowStatus.COMPLETED, …})` → member values. */
function parseTerminalStatuses(source: string, enums: Record<string, string[]>): string[] {
  const match = /^TERMINAL_WORKFLOW_STATUSES = frozenset\(\{([^}]*)\}\)/m.exec(source);
  if (!match) return [];
  const values = enums.WorkflowStatus ?? [];
  return match[1]!
    .split(',')
    .map((part) => part.trim().replace('WorkflowStatus.', '').toLowerCase())
    .filter((name) => values.includes(name));
}

function parsePermClass(source: string): string[] {
  const block = /^class Perm:\n((?:[ \t]+.*\n?)+)/m.exec(source);
  if (!block) return [];
  return Array.from(block[1]!.matchAll(/^\s+[A-Z_]+ = "([^"]+)"/gm), (m) => m[1]!);
}

describe('snapshot matches the live backend source', () => {
  it('parses StrEnum blocks', () => {
    expect(
      parseStrEnums(
        'class A(StrEnum):\n    X = "x"\n    Y_Z = "y_z"\n\n\nOTHER = 1\nclass B(StrEnum):\n    Q = "q"\n',
      ),
    ).toEqual({ A: ['x', 'y_z'], B: ['q'] });
  });

  it.skipIf(!enumsSource)('every snapshotted enum equals app/domain/enums.py', () => {
    const live = parseStrEnums(enumsSource!);
    for (const [name, values] of Object.entries(BACKEND_ENUMS)) {
      expect({ name, values: live[name] }).toEqual({ name, values: [...values] });
    }
    expect(sorted(parseTerminalStatuses(enumsSource!, live))).toEqual(
      sorted(BACKEND_TERMINAL_WORKFLOW_STATUSES),
    );
  });

  it.skipIf(!authzSource)('Perm strings equal app/services/authz.py', () => {
    expect(sorted(parsePermClass(authzSource!))).toEqual(sorted(BACKEND_PERMISSIONS));
  });
});
