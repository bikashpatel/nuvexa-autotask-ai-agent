import dayjs from 'dayjs';
import localizedFormat from 'dayjs/plugin/localizedFormat';
import relativeTime from 'dayjs/plugin/relativeTime';
import utc from 'dayjs/plugin/utc';

import type { ActionType, EmailCategory, Priority, TargetIntegration, Urgency } from '../api/types';
import { lookup } from './lookup';

dayjs.extend(relativeTime);
dayjs.extend(localizedFormat);
dayjs.extend(utc);

export { dayjs };

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = dayjs(value);
  return date.isValid() ? date.format('MMM D, YYYY HH:mm') : '—';
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = dayjs(value);
  return date.isValid() ? date.format('MMM D, YYYY') : '—';
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—';
  const date = dayjs(value);
  return date.isValid() ? date.fromNow() : '—';
}

export function formatIsoTitle(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  const date = dayjs(value);
  return date.isValid() ? date.utc().format('YYYY-MM-DD HH:mm:ss [UTC]') : undefined;
}

const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
const integer = new Intl.NumberFormat('en');

/** Stat-tile values: 1,284 / 12.9K / 4.2M. */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Math.abs(value) < 10_000 ? integer.format(value) : compact.format(value);
}

export function formatInteger(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return integer.format(value);
}

export function formatCurrency(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return new Intl.NumberFormat('en', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: value < 1 ? 4 : 2,
    maximumFractionDigits: value < 1 ? 4 : 2,
  }).format(value);
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function shortHash(hash: string | null | undefined, length = 12): string {
  if (!hash) return '—';
  return hash.slice(0, length);
}

export function shortId(id: string | null | undefined): string {
  if (!id) return '—';
  return id.slice(0, 8);
}

export function humanize(value: string | null | undefined): string {
  if (!value) return '—';
  const text = value.replace(/[_.-]+/g, ' ').trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export const CATEGORY_LABELS: Record<EmailCategory, string> = {
  incident: 'Incident',
  bug: 'Bug',
  service_request: 'Service request',
  task_request: 'Task request',
  feature_request: 'Feature request',
  follow_up: 'Follow-up',
  information_only: 'Information only',
  spam_or_irrelevant: 'Spam / irrelevant',
  other: 'Other',
};

export const PRIORITY_LABELS: Record<Priority, string> = {
  critical: 'Critical',
  high: 'High',
  medium: 'Medium',
  low: 'Low',
};

export const URGENCY_LABELS: Record<Urgency, string> = {
  immediate: 'Immediate',
  high: 'High',
  normal: 'Normal',
  low: 'Low',
};

export const ACTION_TYPE_LABELS: Record<ActionType, string> = {
  create_azure_devops_work_item: 'Azure DevOps work item',
  propose_email_reply: 'Reply draft',
  create_follow_up_task: 'Follow-up task',
  mark_no_action: 'No action',
};

export const TARGET_LABELS: Record<TargetIntegration, string> = {
  azure_devops: 'Azure DevOps',
  reply_drafts: 'Reply drafts (in-app, never sent)',
  internal_tasks: 'NUVEXA tasks',
  none: 'None',
};

export const ADO_PRIORITY_LABELS: Record<number, string> = {
  1: '1 — Highest',
  2: '2 — High',
  3: '3 — Medium',
  4: '4 — Low',
};

export function categoryLabel(value: string | null | undefined): string {
  if (!value) return '—';
  return lookup(CATEGORY_LABELS, value) ?? humanize(value);
}

export function priorityLabel(value: string | null | undefined): string {
  if (!value) return '—';
  return lookup(PRIORITY_LABELS, value) ?? humanize(value);
}

export function actionTypeLabel(value: string | null | undefined): string {
  if (!value) return '—';
  return lookup(ACTION_TYPE_LABELS, value) ?? humanize(value);
}

export function personLabel(name: string | null | undefined, address: string | null | undefined): string {
  if (name && address) return `${name} <${address}>`;
  // `||`, not `??`: Graph can return an empty display name, which must fall back to the address.
  return name || address || 'Unknown sender';
}
