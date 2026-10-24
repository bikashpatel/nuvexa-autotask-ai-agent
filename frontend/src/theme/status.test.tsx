import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  ACTION_STATUSES,
  AUDIT_OUTCOMES,
  EMAIL_ANALYSIS_STATUSES,
  TASK_STATUSES,
  WORKFLOW_STATUSES,
  type ActionStatus,
  type EmailAnalysisStatus,
} from '../api/types';
import { StatusBadge } from '../components/StatusBadge';
import { renderWithProviders } from '../test/utils';
import {
  ACTION_STATUS,
  AUDIT_OUTCOME,
  CONNECTION_STATUS,
  EMAIL_ANALYSIS_STATUS,
  EXECUTION_STATUS,
  IN_FLIGHT_WORKFLOW_STATUSES,
  POLL_INTERVAL_MS,
  STATUS_MAPS,
  SYNC_STATUS,
  TASK_STATUS,
  TERMINAL_WORKFLOW_STATUSES,
  WORKFLOW_STATUS,
  getStatusDescriptor,
  isActionInFlight,
  isEmailInFlight,
  isWorkflowInFlight,
  type StatusDescriptor,
  type StatusKind,
  type StatusTone,
} from './status';

/** Palette contract (file header of status.ts): tone → colour + variant. */
const TONE_STYLE: Record<StatusTone, Pick<StatusDescriptor, 'color' | 'variant'>> = {
  pending: { color: 'orange', variant: 'light' },
  approved: { color: 'blue', variant: 'light' },
  running: { color: 'cyan', variant: 'light' },
  completed: { color: 'green', variant: 'light' },
  failed: { color: 'red', variant: 'light' },
  uncertain: { color: 'grape', variant: 'light' },
  rejected: { color: 'red', variant: 'outline' },
  cancelled: { color: 'gray', variant: 'light' },
  superseded: { color: 'gray', variant: 'default' },
  neutral: { color: 'gray', variant: 'outline' },
  warning: { color: 'yellow', variant: 'light' },
};

type Row = [status: string, label: string, tone: StatusTone, icon: string, loading: boolean];

const ACTION_ROWS: Row[] = [
  ['pending_approval', 'Pending approval', 'pending', 'clock', false],
  ['approved', 'Approved', 'approved', 'check', false],
  ['rejected', 'Rejected', 'rejected', 'x', false],
  ['cancelled', 'Cancelled', 'cancelled', 'ban', false],
  ['executing', 'Executing', 'running', 'loader', true],
  ['completed', 'Completed', 'completed', 'circle-check', false],
  ['failed', 'Failed', 'failed', 'alert', false],
  ['uncertain', 'Uncertain', 'uncertain', 'help', false],
  ['superseded', 'Superseded', 'superseded', 'layers-off', false],
];

const WORKFLOW_ROWS: Row[] = [
  ['received', 'Received', 'running', 'loader', true],
  ['normalized', 'Normalized', 'running', 'loader', true],
  ['classified', 'Classified', 'running', 'loader', true],
  ['extracted', 'Extracted', 'running', 'loader', true],
  ['validated', 'Validated', 'running', 'loader', true],
  ['action_proposed', 'Action proposed', 'running', 'loader', true],
  ['pending_approval', 'Pending approval', 'pending', 'clock', false],
  ['approved', 'Approved', 'approved', 'check', false],
  ['executing', 'Executing', 'running', 'loader', true],
  ['completed', 'Completed', 'completed', 'circle-check', false],
  ['rejected', 'Rejected', 'rejected', 'x', false],
  ['failed', 'Failed', 'failed', 'alert', false],
  ['cancelled', 'Cancelled', 'cancelled', 'ban', false],
];

const EMAIL_ROWS: Row[] = [
  ['not_analyzed', 'Not analyzed', 'neutral', 'circle', false],
  ['queued', 'Queued', 'running', 'clock', false],
  ['analyzing', 'Analyzing', 'running', 'loader', true],
  ['awaiting_review', 'Awaiting review', 'pending', 'clock', false],
  ['done', 'Done', 'completed', 'circle-check', false],
  ['failed', 'Failed', 'failed', 'alert', false],
];

const EXECUTION_ROWS: Row[] = [
  ['running', 'Running', 'running', 'loader', true],
  ['succeeded', 'Succeeded', 'completed', 'circle-check', false],
  ['failed', 'Failed', 'failed', 'alert', false],
  ['uncertain', 'Uncertain', 'uncertain', 'help', false],
];

const CONNECTION_ROWS: Row[] = [
  ['active', 'Active', 'completed', 'plug', false],
  ['needs_reauth', 'Needs re-authentication', 'pending', 'alert', false],
  ['error', 'Error', 'failed', 'alert', false],
  ['disconnected', 'Disconnected', 'cancelled', 'plug-off', false],
];

const SYNC_ROWS: Row[] = [
  ['idle', 'Idle', 'neutral', 'circle', false],
  ['running', 'Syncing', 'running', 'loader', true],
  ['succeeded', 'Synced', 'completed', 'circle-check', false],
  ['failed', 'Sync failed', 'failed', 'alert', false],
];

const TASK_ROWS: Row[] = [
  ['open', 'Open', 'pending', 'clock', false],
  ['done', 'Done', 'completed', 'circle-check', false],
  ['dismissed', 'Dismissed', 'cancelled', 'ban', false],
];

const AUDIT_ROWS: Row[] = [
  ['success', 'Success', 'completed', 'circle-check', false],
  ['failure', 'Failure', 'failed', 'alert', false],
  ['denied', 'Denied', 'rejected', 'ban', false],
];

const TABLES: [StatusKind, Record<string, StatusDescriptor>, Row[]][] = [
  ['action', ACTION_STATUS, ACTION_ROWS],
  ['workflow', WORKFLOW_STATUS, WORKFLOW_ROWS],
  ['email', EMAIL_ANALYSIS_STATUS, EMAIL_ROWS],
  ['execution', EXECUTION_STATUS, EXECUTION_ROWS],
  ['connection', CONNECTION_STATUS, CONNECTION_ROWS],
  ['sync', SYNC_STATUS, SYNC_ROWS],
  ['task', TASK_STATUS, TASK_ROWS],
  ['audit', AUDIT_OUTCOME, AUDIT_ROWS],
];

describe('status mapping', () => {
  it('maps every action status to the agreed colour language', () => {
    expect(ACTION_STATUS.pending_approval).toMatchObject({ color: 'orange', label: 'Pending approval' });
    expect(ACTION_STATUS.approved).toMatchObject({ color: 'blue', label: 'Approved' });
    expect(ACTION_STATUS.executing).toMatchObject({ color: 'cyan', loading: true });
    expect(ACTION_STATUS.completed).toMatchObject({ color: 'green' });
    expect(ACTION_STATUS.failed).toMatchObject({ color: 'red', variant: 'light' });
    expect(ACTION_STATUS.uncertain).toMatchObject({ color: 'grape' });
    expect(ACTION_STATUS.rejected).toMatchObject({ color: 'red', variant: 'outline' });
    expect(ACTION_STATUS.cancelled).toMatchObject({ color: 'gray' });
    expect(ACTION_STATUS.superseded).toMatchObject({ color: 'gray', dimmed: true });
  });

  it('has a label and icon for every enum value (colour is never the only signal)', () => {
    for (const status of ACTION_STATUSES) {
      expect(ACTION_STATUS[status].label).toBeTruthy();
      expect(ACTION_STATUS[status].icon).toBeTruthy();
    }
    for (const status of WORKFLOW_STATUSES) {
      expect(WORKFLOW_STATUS[status].label).toBeTruthy();
    }
  });

  describe.each(TABLES)('%s statuses', (kind, map, rows) => {
    it('has exactly one row per status (no missing or extra keys)', () => {
      expect(Object.keys(map).sort()).toEqual(rows.map(([status]) => status).sort());
    });

    it.each(rows)('%s → %s / %s / %s (loading %s)', (status, label, tone, icon, loading) => {
      const descriptor = map[status]!;
      expect(descriptor).toMatchObject({ label, tone, icon, ...TONE_STYLE[tone] });
      expect(Boolean(descriptor.loading)).toBe(loading);
      // In-flight statuses always pair the loader with the running (cyan) tone.
      if (loading) expect(descriptor.tone).toBe('running');
      expect(Boolean(descriptor.dimmed)).toBe(tone === 'superseded');
      expect(getStatusDescriptor(kind, status)).toBe(descriptor);
    });
  });

  it('every action status carries a reviewer-facing description', () => {
    for (const status of ACTION_STATUSES) {
      expect(ACTION_STATUS[status].description, status).toMatch(/\.$/);
    }
    expect(ACTION_STATUS.uncertain.description).toMatch(/Reconcile before retrying/);
    expect(ACTION_STATUS.approved.description).toMatch(/version and payload hash/);
  });

  it('STATUS_MAPS exposes every kind', () => {
    expect(Object.keys(STATUS_MAPS).sort()).toEqual(
      ['action', 'audit', 'connection', 'email', 'execution', 'sync', 'task', 'workflow'].sort(),
    );
    expect(STATUS_MAPS.task).toBe(TASK_STATUS);
    expect(Object.keys(TASK_STATUS)).toEqual([...TASK_STATUSES]);
    expect(Object.keys(AUDIT_OUTCOME)).toEqual([...AUDIT_OUTCOMES]);
    expect(Object.keys(EMAIL_ANALYSIS_STATUS)).toEqual([...EMAIL_ANALYSIS_STATUSES]);
  });

  it.each(Object.keys(STATUS_MAPS) as StatusKind[])(
    'falls back to a neutral, humanized descriptor for unknown %s values',
    (kind) => {
      expect(getStatusDescriptor(kind, 'brand_new_state')).toEqual({
        label: 'Brand new state',
        tone: 'neutral',
        icon: 'circle',
        color: 'gray',
        variant: 'outline',
      });
    },
  );

  it('humanizes dotted/dashed unknown values and keeps empty or separator-only values', () => {
    expect(getStatusDescriptor('audit', 'policy.denied-twice').label).toBe('Policy denied twice');
    expect(getStatusDescriptor('action', '').label).toBe('');
    expect(getStatusDescriptor('action', '__').label).toBe('__');
    expect(getStatusDescriptor('action', '')).toMatchObject({ tone: 'neutral', color: 'gray' });
  });

  it('does not resolve inherited object keys as statuses (regression: returned a function)', () => {
    for (const key of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      expect(getStatusDescriptor('action', key)).toMatchObject({ tone: 'neutral', icon: 'circle' });
    }
  });
});

describe('in-flight detection (drives polling)', () => {
  it('treats received…action_proposed and executing as in-flight (polling)', () => {
    const inFlight = WORKFLOW_STATUSES.filter((s) => isWorkflowInFlight(s));
    expect(inFlight).toEqual([
      'received',
      'normalized',
      'classified',
      'extracted',
      'validated',
      'action_proposed',
      'executing',
    ]);
    expect([...IN_FLIGHT_WORKFLOW_STATUSES]).toEqual(inFlight);
    expect(isActionInFlight('executing')).toBe(true);
    expect(isActionInFlight('approved')).toBe(false);
    expect(isWorkflowInFlight('pending_approval')).toBe(false);
  });

  it.each(WORKFLOW_STATUSES)('workflow %s: in-flight iff its descriptor shows a loader', (status) => {
    expect(isWorkflowInFlight(status)).toBe(Boolean(WORKFLOW_STATUS[status].loading));
  });

  it.each(ACTION_STATUSES)('action %s: only executing is in flight', (status: ActionStatus) => {
    expect(isActionInFlight(status)).toBe(status === 'executing');
  });

  it.each(EMAIL_ANALYSIS_STATUSES)(
    'email %s: queued and analyzing are in flight',
    (status: EmailAnalysisStatus) => {
      expect(isEmailInFlight(status)).toBe(status === 'queued' || status === 'analyzing');
    },
  );

  it.each([null, undefined])('%s is never in flight', (value) => {
    expect(isWorkflowInFlight(value)).toBe(false);
    expect(isActionInFlight(value)).toBe(false);
    expect(isEmailInFlight(value)).toBe(false);
  });

  it('terminal workflow statuses are completed, rejected and cancelled — never in flight', () => {
    expect([...TERMINAL_WORKFLOW_STATUSES].sort()).toEqual(['cancelled', 'completed', 'rejected']);
    for (const status of TERMINAL_WORKFLOW_STATUSES) {
      expect(isWorkflowInFlight(status)).toBe(false);
    }
    // `failed` is neither terminal nor in flight: it can be re-analyzed.
    expect(TERMINAL_WORKFLOW_STATUSES.has('failed')).toBe(false);
    expect(isWorkflowInFlight('failed')).toBe(false);
  });

  it('polls every 2 seconds', () => {
    expect(POLL_INTERVAL_MS).toBe(2000);
  });
});

describe('<StatusBadge>', () => {
  it('renders the label, an accessible name and the tone', () => {
    renderWithProviders(<StatusBadge kind="action" status="uncertain" />);
    const badge = screen.getByLabelText('Status: Uncertain');
    expect(badge).toHaveTextContent('Uncertain');
    expect(badge).toHaveAttribute('data-tone', 'uncertain');
    expect(badge).toHaveAttribute('data-status', 'uncertain');
  });

  it('renders unknown statuses (even inherited key names) neutrally instead of crashing', () => {
    renderWithProviders(<StatusBadge kind="workflow" status="constructor" />);
    expect(screen.getByLabelText('Status: Constructor')).toHaveAttribute('data-tone', 'neutral');
  });

  it('renders a loader for executing', () => {
    const { container } = renderWithProviders(<StatusBadge kind="action" status="executing" />);
    expect(screen.getByLabelText('Status: Executing')).toBeInTheDocument();
    expect(container.querySelector('.mantine-Loader-root')).not.toBeNull();
  });
});
