import { afterEach, describe, expect, it, vi } from 'vitest';

import { ACTION_TYPES, EMAIL_CATEGORIES, PRIORITIES, URGENCIES } from '../api/types';
import {
  ACTION_TYPE_LABELS,
  ADO_PRIORITY_LABELS,
  CATEGORY_LABELS,
  PRIORITY_LABELS,
  TARGET_LABELS,
  URGENCY_LABELS,
  actionTypeLabel,
  categoryLabel,
  dayjs,
  formatBytes,
  formatCompact,
  formatCurrency,
  formatDate,
  formatDateTime,
  formatDuration,
  formatInteger,
  formatIsoTitle,
  formatLatency,
  formatRelative,
  humanize,
  personLabel,
  priorityLabel,
  shortHash,
  shortId,
} from './format';

const EMPTY = [null, undefined, ''] as const;

afterEach(() => {
  vi.useRealTimers();
});

describe('date formatting', () => {
  // A local (offset-less) timestamp formats identically in every time zone.
  const LOCAL = '2026-10-24T09:05:00';

  it('formatDateTime → "MMM D, YYYY HH:mm"', () => {
    expect(formatDateTime(LOCAL)).toBe('Oct 24, 2026 09:05');
  });

  it('formatDate → "MMM D, YYYY"', () => {
    expect(formatDate(LOCAL)).toBe('Oct 24, 2026');
  });

  it('formatIsoTitle → UTC with seconds', () => {
    expect(formatIsoTitle('2026-10-24T10:00:07Z')).toBe('2026-10-24 10:00:07 UTC');
  });

  it('formatRelative is relative to now', () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date('2026-10-24T12:00:00Z'));
    expect(formatRelative('2026-10-24T10:00:00Z')).toBe('2 hours ago');
    expect(formatRelative('2026-10-24T11:59:30Z')).toBe('a few seconds ago');
    expect(formatRelative('2026-10-27T12:00:00Z')).toBe('in 3 days');
  });

  it.each(EMPTY)('returns an em dash (or undefined for the title) for %j', (value) => {
    expect(formatDateTime(value)).toBe('—');
    expect(formatDate(value)).toBe('—');
    expect(formatRelative(value)).toBe('—');
    expect(formatIsoTitle(value)).toBeUndefined();
  });

  it('treats unparseable dates as missing', () => {
    expect(formatDateTime('not a date')).toBe('—');
    expect(formatDate('not a date')).toBe('—');
    expect(formatRelative('not a date')).toBe('—');
    expect(formatIsoTitle('not a date')).toBeUndefined();
  });

  it('re-exports dayjs with the relativeTime, localizedFormat and utc plugins', () => {
    expect(typeof dayjs.utc).toBe('function');
    expect(typeof dayjs().fromNow).toBe('function');
    expect(dayjs('2026-10-24T09:05:00').format('L')).toBe('10/24/2026');
  });
});

describe('number formatting', () => {
  it.each([
    [0, '0'],
    [1284, '1,284'],
    [9999, '9,999'],
    [10_000, '10K'],
    [12_900, '12.9K'],
    [4_200_000, '4.2M'],
    [-12_000, '-12K'],
    [-9_999, '-9,999'],
  ])('formatCompact(%s) = %s', (value, expected) => {
    expect(formatCompact(value)).toBe(expected);
  });

  it.each([1_234_567, 0, -3])('formatInteger(%s) groups thousands', (value) => {
    expect(formatInteger(value)).toBe(new Intl.NumberFormat('en').format(value));
  });

  it('formatInteger renders 1,234,567', () => {
    expect(formatInteger(1_234_567)).toBe('1,234,567');
  });

  it.each([
    [0.42, '$0.4200'],
    [0, '$0.0000'],
    [0.99999, '$1.0000'],
    [1, '$1.00'],
    [12.5, '$12.50'],
    [1234.567, '$1,234.57'],
  ])('formatCurrency(%s) = %s (4 decimals below $1)', (value, expected) => {
    expect(formatCurrency(value)).toBe(expected);
  });

  it.each([
    [0, '0s'],
    [59.4, '59s'],
    [60, '1m'],
    [90, '2m'],
    [3599, '60m'],
    [3600, '1.0h'],
    [5400, '1.5h'],
    [47.9 * 3600, '47.9h'],
    [48 * 3600, '2.0d'],
    [10 * 86_400, '10.0d'],
  ])('formatDuration(%s) = %s', (value, expected) => {
    expect(formatDuration(value)).toBe(expected);
  });

  it.each([
    [0, '0 ms'],
    [420.4, '420 ms'],
    [999, '999 ms'],
    [1000, '1.00 s'],
    [2100, '2.10 s'],
  ])('formatLatency(%s) = %s', (value, expected) => {
    expect(formatLatency(value)).toBe(expected);
  });

  it.each([
    [0, '0 B'],
    [1023, '1023 B'],
    [1024, '1.0 KB'],
    [2048, '2.0 KB'],
    [1024 * 1024 - 1, '1024.0 KB'],
    [1024 * 1024, '1.0 MB'],
    [5.5 * 1024 * 1024, '5.5 MB'],
  ])('formatBytes(%s) = %s', (value, expected) => {
    expect(formatBytes(value)).toBe(expected);
  });

  it.each([null, undefined, Number.NaN])('numeric formatters render an em dash for %s', (value) => {
    expect(formatCompact(value)).toBe('—');
    expect(formatInteger(value)).toBe('—');
    expect(formatCurrency(value)).toBe('—');
    expect(formatDuration(value)).toBe('—');
    expect(formatLatency(value)).toBe('—');
  });

  it.each([null, undefined])('formatBytes(%s) = —', (value) => {
    expect(formatBytes(value)).toBe('—');
  });
});

describe('identifiers', () => {
  it('shortHash keeps the first 12 characters by default, or a custom length', () => {
    const hash = '0123456789abcdef'.repeat(4);
    expect(shortHash(hash)).toBe('0123456789ab');
    expect(shortHash(hash, 16)).toBe('0123456789abcdef');
    expect(shortHash('abc')).toBe('abc');
  });

  it('shortId keeps the first 8 characters', () => {
    expect(shortId('3f2b8c1e-aaaa-bbbb-cccc-1234567890ab')).toBe('3f2b8c1e');
  });

  it.each(EMPTY)('shortHash/shortId(%j) = —', (value) => {
    expect(shortHash(value)).toBe('—');
    expect(shortId(value)).toBe('—');
  });
});

describe('humanize', () => {
  it.each([
    ['action.approved', 'Action approved'],
    ['needs_reauth', 'Needs reauth'],
    ['await_review', 'Await review'],
    ['step-failed', 'Step failed'],
    ['a__b..c--d', 'A b c d'],
    ['_leading', 'Leading'],
    ['Already', 'Already'],
  ])('humanize(%j) = %j', (value, expected) => {
    expect(humanize(value)).toBe(expected);
  });

  it.each(EMPTY)('humanize(%j) = —', (value) => {
    expect(humanize(value)).toBe('—');
  });
});

describe('label maps', () => {
  it('CATEGORY_LABELS has the agreed label for every category', () => {
    expect(CATEGORY_LABELS).toEqual({
      incident: 'Incident',
      bug: 'Bug',
      service_request: 'Service request',
      task_request: 'Task request',
      feature_request: 'Feature request',
      follow_up: 'Follow-up',
      information_only: 'Information only',
      spam_or_irrelevant: 'Spam / irrelevant',
      other: 'Other',
    });
    for (const category of EMAIL_CATEGORIES) expect(categoryLabel(category)).toBe(CATEGORY_LABELS[category]);
  });

  it('PRIORITY_LABELS / URGENCY_LABELS cover every value', () => {
    expect(PRIORITY_LABELS).toEqual({ critical: 'Critical', high: 'High', medium: 'Medium', low: 'Low' });
    expect(URGENCY_LABELS).toEqual({ immediate: 'Immediate', high: 'High', normal: 'Normal', low: 'Low' });
    for (const priority of PRIORITIES) expect(priorityLabel(priority)).toBe(PRIORITY_LABELS[priority]);
    for (const urgency of URGENCIES) expect(URGENCY_LABELS[urgency]).toBeTruthy();
  });

  it('ACTION_TYPE_LABELS / TARGET_LABELS cover every value; reply drafts are explicitly "never sent"', () => {
    expect(ACTION_TYPE_LABELS).toEqual({
      create_azure_devops_work_item: 'Azure DevOps work item',
      propose_email_reply: 'Reply draft',
      create_follow_up_task: 'Follow-up task',
      mark_no_action: 'No action',
    });
    for (const type of ACTION_TYPES) expect(actionTypeLabel(type)).toBe(ACTION_TYPE_LABELS[type]);
    expect(TARGET_LABELS).toEqual({
      azure_devops: 'Azure DevOps',
      reply_drafts: 'Reply drafts (in-app, never sent)',
      internal_tasks: 'NUVEXA tasks',
      none: 'None',
    });
  });

  it('ADO_PRIORITY_LABELS maps 1–4 with 1 = highest', () => {
    expect(ADO_PRIORITY_LABELS).toEqual({
      1: '1 — Highest',
      2: '2 — High',
      3: '3 — Medium',
      4: '4 — Low',
    });
  });

  it('label functions humanize unknown (forward-compatible) values', () => {
    expect(categoryLabel('security_incident')).toBe('Security incident');
    expect(priorityLabel('blocker')).toBe('Blocker');
    expect(actionTypeLabel('create_jira_issue')).toBe('Create jira issue');
  });

  it.each(EMPTY)('label functions render — for %j', (value) => {
    expect(categoryLabel(value)).toBe('—');
    expect(priorityLabel(value)).toBe('—');
    expect(actionTypeLabel(value)).toBe('—');
  });
});

describe('personLabel', () => {
  it('combines name and address', () => {
    expect(personLabel('Dana Customer', 'dana@fabrikam.example')).toBe(
      'Dana Customer <dana@fabrikam.example>',
    );
  });

  it('falls back to whichever part exists', () => {
    expect(personLabel('Dana Customer', null)).toBe('Dana Customer');
    expect(personLabel(null, 'dana@fabrikam.example')).toBe('dana@fabrikam.example');
    expect(personLabel(undefined, undefined)).toBe('Unknown sender');
  });

  it('treats an empty display name as missing (regression: showed a blank sender)', () => {
    expect(personLabel('', 'dana@fabrikam.example')).toBe('dana@fabrikam.example');
    expect(personLabel('', '')).toBe('Unknown sender');
    expect(personLabel('Dana', '')).toBe('Dana');
  });

  it('keeps hostile display names as inert text', () => {
    expect(personLabel('<img src=x onerror=alert(1)>', 'x@y.example')).toBe(
      '<img src=x onerror=alert(1)> <x@y.example>',
    );
  });
});
