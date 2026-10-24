/**
 * Form model + client-side validation for editable action payloads.
 *
 * Limits mirror app/schemas/actions.py exactly (the server re-validates everything; this only
 * gives fast feedback). Strings are compared after trimming because the API models use
 * `str_strip_whitespace=True`.
 */
import dayjs from 'dayjs';

import {
  ACTION_TYPES,
  type AzureDevOpsWorkItemPayload,
  type EmailReplyDraftPayload,
  type FollowUpTaskPayload,
  type NoActionPayload,
  type SourceEmailReference,
} from '../../api/types';

export const LIMITS = {
  ado: {
    project: 64,
    workItemType: 128,
    title: 255,
    description: 32_000,
    path: 256,
    tags: 10,
    tag: 64,
    additionalFields: 10,
    additionalFieldValue: 4_000,
  },
  reply: { subject: 255, body: 20_000 },
  followUp: { title: 255, description: 10_000 },
  noAction: { reason: 1_000 },
  approveComment: 1_000,
  rejectReason: 1_000,
  cancelReason: 1_000,
} as const;

// Python's `\w` is Unicode-aware for str patterns → letters, digits (any script), marks and "_".
const TAG_RE = /^[\p{L}\p{N}\p{M}_ .\-:/]{1,64}$/u;
const FIELD_REF_RE = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$/;
// eslint-disable-next-line no-control-regex
const PATH_RE = /^[^<>|"*?:\u0000-\u001f]{1,256}$/;
const EMAIL_RE = /^[^\s@<>()[\]\\,;:"]+@[^\s@<>()[\]\\,;:"]+\.[^\s@<>()[\]\\,;:"]{2,}$/;

export const DATE_TIME_FORMAT = 'YYYY-MM-DD HH:mm:ss';

export type Errors = Record<string, string | null>;

function requiredMax(value: string, max: number, label: string): string | null {
  const v = value.trim();
  if (!v) return `${label} is required`;
  if (v.length > max) return `${label} must be at most ${max} characters (currently ${v.length})`;
  return null;
}

function optionalMax(value: string, max: number, label: string): string | null {
  const v = value.trim();
  if (v.length > max) return `${label} must be at most ${max} characters (currently ${v.length})`;
  return null;
}

function emptySource(): SourceEmailReference {
  return { email_id: '' };
}

// --- Azure DevOps work item ----------------------------------------------------------------------

export interface AdditionalFieldRow {
  name: string;
  value: string;
  /** The original value was numeric — send it back as a number when it still parses. */
  numeric: boolean;
}

export interface AdoFormValues {
  project: string;
  work_item_type: string;
  title: string;
  description: string;
  priority: string;
  area_path: string;
  iteration_path: string;
  tags: string[];
  assigned_to: string;
  additional_fields: AdditionalFieldRow[];
}

export function adoToForm(payload: Partial<AzureDevOpsWorkItemPayload>): AdoFormValues {
  const priority = Number(payload.priority);
  return {
    project: payload.project ?? '',
    work_item_type: payload.work_item_type ?? '',
    title: payload.title ?? '',
    description: payload.description ?? '',
    priority: priority >= 1 && priority <= 4 ? String(priority) : '2',
    area_path: payload.area_path ?? '',
    iteration_path: payload.iteration_path ?? '',
    tags: Array.isArray(payload.tags) ? [...payload.tags] : [],
    assigned_to: payload.assigned_to ?? '',
    additional_fields: Object.entries(payload.additional_fields ?? {}).map(([name, value]) => ({
      name,
      value: String(value),
      numeric: typeof value === 'number',
    })),
  };
}

export function validateTag(tag: string): string | null {
  const t = tag.trim();
  if (!t) return null;
  if (t.includes(';')) return `Tag “${t}” must not contain “;”`;
  if (t.length > LIMITS.ado.tag)
    return `Tag “${t.slice(0, 20)}…” must be at most ${LIMITS.ado.tag} characters`;
  if (!TAG_RE.test(t)) return `Tag “${t}” may only contain letters, digits, spaces and _ . - : /`;
  return null;
}

export function validatePath(value: string, label: string): string | null {
  const v = value.trim();
  if (!v) return null;
  if (v.length > LIMITS.ado.path) return `${label} must be at most ${LIMITS.ado.path} characters`;
  if (!PATH_RE.test(v))
    return `${label} contains characters that are not allowed (< > | " * ? : or control characters)`;
  return null;
}

export function validateAdo(values: AdoFormValues): Errors {
  const errors: Errors = {};
  errors.project = requiredMax(values.project, LIMITS.ado.project, 'Project');
  errors.work_item_type = requiredMax(values.work_item_type, LIMITS.ado.workItemType, 'Work item type');
  errors.title = requiredMax(values.title, LIMITS.ado.title, 'Title');
  errors.description = optionalMax(values.description, LIMITS.ado.description, 'Description');

  const priority = Number(values.priority);
  errors.priority =
    Number.isInteger(priority) && priority >= 1 && priority <= 4 ? null : 'Priority must be 1–4';

  errors.area_path = validatePath(values.area_path, 'Area path');
  errors.iteration_path = validatePath(values.iteration_path, 'Iteration path');

  const tags = values.tags.map((t) => t.trim()).filter(Boolean);
  if (new Set(tags).size > LIMITS.ado.tags) {
    errors.tags = `At most ${LIMITS.ado.tags} tags`;
  } else {
    errors.tags = tags.map(validateTag).find((e) => e) ?? null;
  }

  const assignee = values.assigned_to.trim();
  errors.assigned_to = assignee && !EMAIL_RE.test(assignee) ? 'Assigned to must be an email address' : null;

  const rows = values.additional_fields.filter((row) => row.name.trim() || row.value.trim());
  if (rows.length > LIMITS.ado.additionalFields) {
    errors.additional_fields = `At most ${LIMITS.ado.additionalFields} additional fields`;
  }
  const seen = new Set<string>();
  values.additional_fields.forEach((row, index) => {
    const name = row.name.trim();
    if (!name && !row.value.trim()) return;
    if (!name) {
      errors[`additional_fields.${index}.name`] = 'Field reference name is required';
    } else if (!FIELD_REF_RE.test(name)) {
      errors[`additional_fields.${index}.name`] =
        'Use a field reference name, e.g. Microsoft.VSTS.Common.Severity';
    } else if (seen.has(name)) {
      errors[`additional_fields.${index}.name`] = 'Duplicate field';
    }
    seen.add(name);
    if (row.value.length > LIMITS.ado.additionalFieldValue) {
      errors[`additional_fields.${index}.value`] =
        `Value must be at most ${LIMITS.ado.additionalFieldValue} characters`;
    }
  });
  return errors;
}

export function adoToPayload(
  values: AdoFormValues,
  original: Partial<AzureDevOpsWorkItemPayload>,
): AzureDevOpsWorkItemPayload {
  const additional: Record<string, string | number> = {};
  for (const row of values.additional_fields) {
    const name = row.name.trim();
    if (!name) continue;
    const asNumber = Number(row.value);
    additional[name] =
      row.numeric && row.value.trim() !== '' && Number.isFinite(asNumber) ? asNumber : row.value;
  }
  return {
    ...original,
    kind: 'create_azure_devops_work_item',
    project: values.project.trim(),
    work_item_type: values.work_item_type.trim(),
    title: values.title.trim(),
    description: values.description.trim(),
    priority: Number(values.priority),
    area_path: values.area_path.trim() || null,
    iteration_path: values.iteration_path.trim() || null,
    tags: Array.from(new Set(values.tags.map((t) => t.trim()).filter(Boolean))),
    assigned_to: values.assigned_to.trim() || null,
    additional_fields: additional,
    source_email: original.source_email ?? emptySource(),
  };
}

// --- Email reply draft ---------------------------------------------------------------------------

export interface ReplyFormValues {
  subject: string;
  body: string;
}

export function replyToForm(payload: Partial<EmailReplyDraftPayload>): ReplyFormValues {
  return { subject: payload.subject ?? '', body: payload.body ?? '' };
}

export function validateReply(values: ReplyFormValues): Errors {
  return {
    subject: requiredMax(values.subject, LIMITS.reply.subject, 'Subject'),
    body: requiredMax(values.body, LIMITS.reply.body, 'Body'),
  };
}

export function replyToPayload(
  values: ReplyFormValues,
  original: Partial<EmailReplyDraftPayload>,
): EmailReplyDraftPayload {
  return {
    ...original,
    kind: 'propose_email_reply',
    // Recipient is read-only: it is always forced to the original sender by the server.
    to: original.to ?? [],
    subject: values.subject.trim(),
    body: values.body.trim(),
    in_reply_to_internet_message_id: original.in_reply_to_internet_message_id ?? null,
    source_email: original.source_email ?? emptySource(),
  };
}

// --- Follow-up task ------------------------------------------------------------------------------

export interface FollowUpFormValues {
  title: string;
  description: string;
  /** Local date-time string (YYYY-MM-DD HH:mm:ss) as produced by the date picker, or null. */
  due_at: string | null;
}

export function followUpToForm(payload: Partial<FollowUpTaskPayload>): FollowUpFormValues {
  const due = payload.due_at ? dayjs(payload.due_at) : null;
  return {
    title: payload.title ?? '',
    description: payload.description ?? '',
    due_at: due?.isValid() ? due.format(DATE_TIME_FORMAT) : null,
  };
}

export function validateFollowUp(values: FollowUpFormValues): Errors {
  return {
    title: requiredMax(values.title, LIMITS.followUp.title, 'Title'),
    description: optionalMax(values.description, LIMITS.followUp.description, 'Description'),
    due_at: values.due_at && !dayjs(values.due_at).isValid() ? 'Due date is not a valid date' : null,
  };
}

export function followUpToPayload(
  values: FollowUpFormValues,
  original: Partial<FollowUpTaskPayload>,
): FollowUpTaskPayload {
  return {
    ...original,
    kind: 'create_follow_up_task',
    title: values.title.trim(),
    description: values.description.trim(),
    due_at: values.due_at ? dayjs(values.due_at).toISOString() : null,
    source_email: original.source_email ?? emptySource(),
  };
}

// --- No action -----------------------------------------------------------------------------------

export interface NoActionFormValues {
  reason: string;
}

export function noActionToForm(payload: Partial<NoActionPayload>): NoActionFormValues {
  return { reason: payload.reason ?? '' };
}

export function validateNoAction(values: NoActionFormValues): Errors {
  return { reason: requiredMax(values.reason, LIMITS.noAction.reason, 'Reason') };
}

export function noActionToPayload(
  values: NoActionFormValues,
  original: Partial<NoActionPayload>,
): NoActionPayload {
  return {
    ...original,
    kind: 'mark_no_action',
    reason: values.reason.trim(),
    source_email: original.source_email ?? emptySource(),
  };
}

// --- Shared --------------------------------------------------------------------------------------

export function hasErrors(errors: Errors): boolean {
  return Object.values(errors).some(Boolean);
}

/** Drop null entries (the shape @mantine/form's `validate` function expects). */
export function compactErrors(errors: Errors): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(errors)) if (value) out[key] = value;
  return out;
}

const ACTION_KINDS: readonly string[] = ACTION_TYPES;

/**
 * Map server 422 field paths onto form field names. Pydantic discriminated-union errors are
 * prefixed with the payload kind (e.g. `create_azure_devops_work_item.title`), which is stripped.
 */
export function mapServerFieldErrors(
  fieldErrors: { field: string; message: string }[],
  knownFields: readonly string[],
): { fieldErrors: Record<string, string>; general: string[] } {
  const mapped: Record<string, string> = {};
  const general: string[] = [];
  for (const { field, message } of fieldErrors) {
    const segments = field.split('.').filter(Boolean);
    while (segments.length > 0 && ACTION_KINDS.includes(segments[0] ?? '')) segments.shift();
    const [head] = segments;
    if (head && knownFields.includes(head)) {
      mapped[head] = mapped[head] ? `${mapped[head]}; ${message}` : message;
    } else {
      general.push(field ? `${field}: ${message}` : message);
    }
  }
  return { fieldErrors: mapped, general };
}
