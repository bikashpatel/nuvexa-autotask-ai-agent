import {
  EMAIL_CATEGORIES,
  PRIORITIES,
  type EmailCategory,
  type Priority,
  type TenantSettings,
} from '../../api/types';

/** Form shape: nullable strings become '' so inputs stay controlled. */
export interface SettingsFormValues {
  azure_devops: {
    project: string;
    work_item_type: string;
    area_path: string;
    iteration_path: string;
    allowed_projects: string[];
    allowed_work_item_types: string[];
    allowed_additional_fields: string[];
    category_work_item_types: Record<EmailCategory, string>;
  };
  taxonomy: {
    enabled: Record<EmailCategory, boolean>;
    category_descriptions: Record<EmailCategory, string>;
  };
  priority: {
    category_default_priority: Record<EmailCategory, Priority>;
    azure_devops_priority: Record<Priority, string>;
  };
  confidence: { high: number; medium: number };
  retention: { email_body_days: number; workflow_event_days: number; audit_days: number };
  auto_analyze_new_emails: boolean;
  attachment_processing_enabled: boolean;
}

function byCategory<T>(fn: (c: EmailCategory) => T): Record<EmailCategory, T> {
  return Object.fromEntries(EMAIL_CATEGORIES.map((c) => [c, fn(c)])) as Record<EmailCategory, T>;
}

export function settingsToForm(s: TenantSettings): SettingsFormValues {
  const enabled = new Set(s.taxonomy.enabled_categories);
  return {
    azure_devops: {
      project: s.azure_devops.project ?? '',
      work_item_type: s.azure_devops.work_item_type ?? '',
      area_path: s.azure_devops.area_path ?? '',
      iteration_path: s.azure_devops.iteration_path ?? '',
      allowed_projects: [...s.azure_devops.allowed_projects],
      allowed_work_item_types: [...s.azure_devops.allowed_work_item_types],
      allowed_additional_fields: [...s.azure_devops.allowed_additional_fields],
      category_work_item_types: byCategory((c) => s.azure_devops.category_work_item_types[c] ?? ''),
    },
    taxonomy: {
      enabled: byCategory((c) => c === 'other' || enabled.has(c)),
      category_descriptions: byCategory((c) => s.taxonomy.category_descriptions[c] ?? ''),
    },
    priority: {
      category_default_priority: byCategory((c) => s.priority.category_default_priority[c] ?? 'low'),
      azure_devops_priority: Object.fromEntries(
        PRIORITIES.map((p) => [p, String(s.priority.azure_devops_priority[p] ?? 2)]),
      ) as Record<Priority, string>,
    },
    confidence: { high: s.confidence.high, medium: s.confidence.medium },
    retention: { ...s.retention },
    auto_analyze_new_emails: s.auto_analyze_new_emails,
    attachment_processing_enabled: s.attachment_processing_enabled,
  };
}

export function formToSettings(v: SettingsFormValues): TenantSettings {
  const categoryTypes: Partial<Record<EmailCategory, string>> = {};
  for (const c of EMAIL_CATEGORIES) {
    const value = v.azure_devops.category_work_item_types[c]?.trim();
    if (value) categoryTypes[c] = value;
  }
  const descriptions: Partial<Record<EmailCategory, string>> = {};
  for (const c of EMAIL_CATEGORIES) {
    const value = v.taxonomy.category_descriptions[c]?.trim();
    if (value) descriptions[c] = value;
  }
  const clean = (list: string[]) => Array.from(new Set(list.map((x) => x.trim()).filter(Boolean)));
  return {
    azure_devops: {
      project: v.azure_devops.project.trim() || null,
      work_item_type: v.azure_devops.work_item_type.trim() || 'Task',
      area_path: v.azure_devops.area_path.trim() || null,
      iteration_path: v.azure_devops.iteration_path.trim() || null,
      allowed_projects: clean(v.azure_devops.allowed_projects),
      allowed_work_item_types: clean(v.azure_devops.allowed_work_item_types),
      allowed_additional_fields: clean(v.azure_devops.allowed_additional_fields),
      category_work_item_types: categoryTypes,
    },
    taxonomy: {
      // "other" is the fallback category and can never be disabled.
      enabled_categories: EMAIL_CATEGORIES.filter((c) => c === 'other' || v.taxonomy.enabled[c]),
      category_descriptions: descriptions,
    },
    priority: {
      category_default_priority: { ...v.priority.category_default_priority },
      azure_devops_priority: Object.fromEntries(
        PRIORITIES.map((p) => [p, Number(v.priority.azure_devops_priority[p])]),
      ) as Record<Priority, number>,
    },
    confidence: { high: Number(v.confidence.high), medium: Number(v.confidence.medium) },
    retention: {
      email_body_days: Number(v.retention.email_body_days),
      workflow_event_days: Number(v.retention.workflow_event_days),
      audit_days: Number(v.retention.audit_days),
    },
    auto_analyze_new_emails: v.auto_analyze_new_emails,
    attachment_processing_enabled: false,
  };
}

function range(value: number, min: number, max: number, label: string): string | null {
  if (typeof value !== 'number' || Number.isNaN(value)) return `${label} is required`;
  if (!Number.isInteger(value)) return `${label} must be a whole number`;
  if (value < min || value > max) return `${label} must be between ${min} and ${max}`;
  return null;
}

/** Mirrors app/schemas/settings.py validators and field limits. */
export function validateSettings(v: SettingsFormValues): Record<string, string> {
  const errors: Record<string, string | null> = {};
  const ado = v.azure_devops;
  errors['azure_devops.project'] = ado.project.trim().length > 64 ? 'At most 64 characters' : null;
  errors['azure_devops.work_item_type'] =
    ado.work_item_type.trim().length > 128 ? 'At most 128 characters' : null;
  errors['azure_devops.area_path'] = ado.area_path.trim().length > 256 ? 'At most 256 characters' : null;
  errors['azure_devops.iteration_path'] =
    ado.iteration_path.trim().length > 256 ? 'At most 256 characters' : null;
  errors['azure_devops.allowed_projects'] = ado.allowed_projects.length > 100 ? 'At most 100 projects' : null;

  for (const p of PRIORITIES) {
    const n = Number(v.priority.azure_devops_priority[p]);
    if (![1, 2, 3, 4].includes(n)) errors[`priority.azure_devops_priority.${p}`] = 'Must be 1–4';
  }

  const { high, medium } = v.confidence;
  errors['confidence.high'] = high < 0 || high > 1 || Number.isNaN(high) ? 'Must be between 0 and 1' : null;
  errors['confidence.medium'] =
    medium < 0 || medium > 1 || Number.isNaN(medium)
      ? 'Must be between 0 and 1'
      : medium > high
        ? 'Medium threshold must be ≤ high threshold'
        : null;

  errors['retention.email_body_days'] = range(v.retention.email_body_days, 1, 3650, 'Email body retention');
  errors['retention.workflow_event_days'] = range(
    v.retention.workflow_event_days,
    1,
    3650,
    'Workflow event retention',
  );
  errors['retention.audit_days'] = range(v.retention.audit_days, 30, 3650, 'Audit retention');

  const out: Record<string, string> = {};
  for (const [k, val] of Object.entries(errors)) if (val) out[k] = val;
  return out;
}
