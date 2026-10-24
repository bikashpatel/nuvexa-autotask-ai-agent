import { describe, expect, it } from 'vitest';

import { EMAIL_CATEGORIES, type TenantSettings } from '../../api/types';
import { settingsOut } from '../../test/fixtures';
import { formToSettings, settingsToForm, validateSettings, type SettingsFormValues } from './settingsForm';

const tenant = (): TenantSettings => settingsOut().settings;

function form(mutate: (v: SettingsFormValues) => void = () => undefined): SettingsFormValues {
  const values = settingsToForm(tenant());
  mutate(values);
  return values;
}

describe('settingsToForm', () => {
  it('turns nullable strings into empty strings and copies the lists', () => {
    const s = tenant();
    const values = settingsToForm(s);
    expect(values.azure_devops).toMatchObject({
      project: 'Payments',
      work_item_type: 'Task',
      area_path: '',
      iteration_path: '',
      allowed_projects: [],
      allowed_work_item_types: ['Bug', 'Task', 'User Story'],
      allowed_additional_fields: ['Microsoft.VSTS.Common.Severity'],
    });
    // Copies, not shared references (form edits must not mutate the cache).
    expect(values.azure_devops.allowed_work_item_types).not.toBe(s.azure_devops.allowed_work_item_types);
    expect(values.retention).not.toBe(s.retention);
  });

  it('expands per-category records with defaults for every category', () => {
    const values = settingsToForm(tenant());
    for (const c of EMAIL_CATEGORIES) {
      expect(values.azure_devops.category_work_item_types[c]).toBe(
        c === 'incident' || c === 'bug' ? 'Bug' : '',
      );
      expect(values.priority.category_default_priority[c]).toBe(c === 'incident' ? 'high' : 'low');
      expect(values.taxonomy.category_descriptions[c]).toBe(c === 'incident' ? 'Production outage.' : '');
    }
    expect(values.taxonomy.enabled).toMatchObject({
      incident: true,
      bug: true,
      other: true,
      follow_up: false,
    });
    expect(values.priority.azure_devops_priority).toEqual({
      critical: '1',
      high: '1',
      medium: '2',
      low: '3',
    });
  });

  it('always enables "other" and defaults missing values', () => {
    const s = tenant();
    s.taxonomy.enabled_categories = [];
    s.azure_devops.project = null;
    // Simulate an API response missing optional mappings.
    s.priority.azure_devops_priority = {} as TenantSettings['priority']['azure_devops_priority'];
    s.azure_devops.work_item_type = undefined as unknown as string;
    const values = settingsToForm(s);
    expect(values.taxonomy.enabled.other).toBe(true);
    expect(values.taxonomy.enabled.incident).toBe(false);
    expect(values.azure_devops.project).toBe('');
    expect(values.azure_devops.work_item_type).toBe('');
    expect(values.priority.azure_devops_priority).toEqual({
      critical: '2',
      high: '2',
      medium: '2',
      low: '2',
    });
  });
});

describe('formToSettings', () => {
  it('round-trips the API settings', () => {
    const s = tenant();
    const back = formToSettings(settingsToForm(s));
    expect(back).toEqual({
      ...s,
      priority: {
        // Every category gets an explicit default priority after editing.
        category_default_priority: Object.fromEntries(
          EMAIL_CATEGORIES.map((c) => [c, c === 'incident' ? 'high' : 'low']),
        ),
        azure_devops_priority: s.priority.azure_devops_priority,
      },
    });
  });

  it('trims, maps empty strings to null / "Task", and dedupes lists', () => {
    const out = formToSettings(
      form((v) => {
        v.azure_devops.project = '   ';
        v.azure_devops.work_item_type = '  ';
        v.azure_devops.area_path = ' Payments\\Checkout ';
        v.azure_devops.iteration_path = '';
        v.azure_devops.allowed_projects = [' Payments ', 'Payments', '', 'Ops'];
        v.azure_devops.allowed_work_item_types = ['Bug', ' Bug'];
        v.azure_devops.allowed_additional_fields = ['  '];
      }),
    );
    expect(out.azure_devops).toMatchObject({
      project: null,
      work_item_type: 'Task',
      area_path: 'Payments\\Checkout',
      iteration_path: null,
      allowed_projects: ['Payments', 'Ops'],
      allowed_work_item_types: ['Bug'],
      allowed_additional_fields: [],
    });
  });

  it('drops empty category work item types and descriptions, keeping trimmed ones', () => {
    const out = formToSettings(
      form((v) => {
        v.azure_devops.category_work_item_types.bug = '  ';
        v.azure_devops.category_work_item_types.feature_request = ' User Story ';
        v.taxonomy.category_descriptions.incident = '   ';
        v.taxonomy.category_descriptions.follow_up = ' Chasers ';
      }),
    );
    expect(out.azure_devops.category_work_item_types).toEqual({
      incident: 'Bug',
      feature_request: 'User Story',
    });
    expect(out.taxonomy.category_descriptions).toEqual({ follow_up: 'Chasers' });
  });

  it('keeps enabled categories in enum order and never disables "other"', () => {
    const out = formToSettings(
      form((v) => {
        v.taxonomy.enabled.other = false;
        v.taxonomy.enabled.follow_up = true;
        v.taxonomy.enabled.incident = false;
      }),
    );
    expect(out.taxonomy.enabled_categories).toEqual(['bug', 'follow_up', 'other']);
  });

  it('converts numeric strings, forces attachment processing off and keeps auto-analyze', () => {
    const out = formToSettings(
      form((v) => {
        v.priority.azure_devops_priority.low = '4';
        v.confidence.high = '0.8' as unknown as number;
        v.retention.audit_days = '400' as unknown as number;
        v.auto_analyze_new_emails = true;
        v.attachment_processing_enabled = true;
      }),
    );
    expect(out.priority.azure_devops_priority).toEqual({ critical: 1, high: 1, medium: 2, low: 4 });
    expect(out.confidence).toEqual({ high: 0.8, medium: 0.5 });
    expect(out.retention.audit_days).toBe(400);
    expect(out.auto_analyze_new_emails).toBe(true);
    expect(out.attachment_processing_enabled).toBe(false);
  });
});

describe('validateSettings (mirrors app/schemas/settings.py)', () => {
  it('accepts the default settings', () => {
    expect(validateSettings(form())).toEqual({});
  });

  it.each([
    ['project', 64],
    ['work_item_type', 128],
    ['area_path', 256],
    ['iteration_path', 256],
  ] as const)('limits azure_devops.%s to %i characters (after trimming)', (field, max) => {
    expect(
      validateSettings(form((v) => (v.azure_devops[field] = `  ${'x'.repeat(max)}  `)))[
        `azure_devops.${field}`
      ],
    ).toBeUndefined();
    expect(validateSettings(form((v) => (v.azure_devops[field] = 'x'.repeat(max + 1))))).toEqual({
      [`azure_devops.${field}`]: `At most ${max} characters`,
    });
  });

  it('limits allowed projects to 100', () => {
    const projects = (n: number) => Array.from({ length: n }, (_, i) => `P${i}`);
    expect(validateSettings(form((v) => (v.azure_devops.allowed_projects = projects(100))))).toEqual({});
    expect(validateSettings(form((v) => (v.azure_devops.allowed_projects = projects(101))))).toEqual({
      'azure_devops.allowed_projects': 'At most 100 projects',
    });
  });

  it('requires every Azure DevOps priority to be 1–4', () => {
    const errors = validateSettings(
      form((v) => {
        v.priority.azure_devops_priority.critical = '0';
        v.priority.azure_devops_priority.low = '5';
        v.priority.azure_devops_priority.medium = 'x';
        v.priority.azure_devops_priority.high = '4';
      }),
    );
    expect(errors).toEqual({
      'priority.azure_devops_priority.critical': 'Must be 1–4',
      'priority.azure_devops_priority.medium': 'Must be 1–4',
      'priority.azure_devops_priority.low': 'Must be 1–4',
    });
  });

  it('keeps confidence thresholds within 0–1 with medium ≤ high', () => {
    const conf = (high: number, medium: number) =>
      validateSettings(form((v) => (v.confidence = { high, medium })));
    expect(conf(1, 0)).toEqual({});
    expect(conf(0.5, 0.5)).toEqual({});
    expect(conf(1.01, 0.5)).toEqual({ 'confidence.high': 'Must be between 0 and 1' });
    expect(conf(-0.1, -0.2)).toEqual({
      'confidence.high': 'Must be between 0 and 1',
      'confidence.medium': 'Must be between 0 and 1',
    });
    expect(conf(Number.NaN, Number.NaN)).toEqual({
      'confidence.high': 'Must be between 0 and 1',
      'confidence.medium': 'Must be between 0 and 1',
    });
    expect(conf(0.5, 0.6)).toEqual({ 'confidence.medium': 'Medium threshold must be ≤ high threshold' });
    expect(conf(0.5, 1.5)).toEqual({ 'confidence.medium': 'Must be between 0 and 1' });
  });

  it('rejects an emptied confidence input instead of saving it as 0 (regression)', () => {
    const empty = '' as unknown as number;
    const conf = (high: number, medium: number) =>
      validateSettings(form((v) => (v.confidence = { high, medium })));
    expect(conf(0.75, empty)).toEqual({ 'confidence.medium': 'Must be between 0 and 1' });
    // Only the emptied field is flagged (no misleading "medium ≤ high" error).
    expect(conf(empty, 0.5)).toEqual({ 'confidence.high': 'Must be between 0 and 1' });
  });

  it.each([
    ['email_body_days', 1, 'Email body retention'],
    ['workflow_event_days', 1, 'Workflow event retention'],
    ['audit_days', 30, 'Audit retention'],
  ] as const)('validates retention.%s between %i and 3650 days', (field, min, label) => {
    const check = (value: number) => validateSettings(form((v) => (v.retention[field] = value)));
    expect(check(min)).toEqual({});
    expect(check(3650)).toEqual({});
    expect(check(min - 1)).toEqual({ [`retention.${field}`]: `${label} must be between ${min} and 3650` });
    expect(check(3651)).toEqual({ [`retention.${field}`]: `${label} must be between ${min} and 3650` });
    expect(check(min + 0.5)).toEqual({ [`retention.${field}`]: `${label} must be a whole number` });
    expect(check(Number.NaN)).toEqual({ [`retention.${field}`]: `${label} is required` });
    // An emptied NumberInput yields '' (not a number).
    expect(check('' as unknown as number)).toEqual({ [`retention.${field}`]: `${label} is required` });
  });
});
