import { describe, expect, it } from 'vitest';

import type { AzureDevOpsWorkItemPayload } from '../../api/types';
import {
  adoToForm,
  adoToPayload,
  compactErrors,
  followUpToForm,
  followUpToPayload,
  hasErrors,
  mapServerFieldErrors,
  noActionToForm,
  noActionToPayload,
  replyToForm,
  replyToPayload,
  validateAdo,
  validateFollowUp,
  validateNoAction,
  validatePath,
  validateReply,
  validateTag,
  type AdoFormValues,
} from './payloadForms';

const base: AzureDevOpsWorkItemPayload = {
  kind: 'create_azure_devops_work_item',
  project: 'Payments',
  work_item_type: 'Bug',
  title: 'Checkout fails',
  description: 'desc',
  priority: 2,
  area_path: null,
  iteration_path: null,
  tags: ['checkout'],
  assigned_to: null,
  additional_fields: { 'Microsoft.VSTS.Common.Severity': '2 - High', 'Custom.Score': 7 },
  source_email: { email_id: 'email-1', subject: 'Subject' },
};

function form(overrides: Partial<AdoFormValues> = {}): AdoFormValues {
  return { ...adoToForm(base), ...overrides };
}

describe('Azure DevOps payload validation (mirrors app/schemas/actions.py)', () => {
  it('accepts a valid payload', () => {
    expect(hasErrors(validateAdo(form()))).toBe(false);
  });

  it('requires project, work item type and title (after trimming)', () => {
    const errors = validateAdo(form({ project: '  ', work_item_type: '', title: '   ' }));
    expect(errors.project).toMatch(/required/);
    expect(errors.work_item_type).toMatch(/required/);
    expect(errors.title).toMatch(/required/);
  });

  it('enforces string limits: project 64, type 128, title 255, description 32000', () => {
    const errors = validateAdo(
      form({
        project: 'p'.repeat(65),
        work_item_type: 't'.repeat(129),
        title: 'x'.repeat(256),
        description: 'd'.repeat(32_001),
      }),
    );
    expect(errors.project).toMatch(/at most 64/);
    expect(errors.work_item_type).toMatch(/at most 128/);
    expect(errors.title).toMatch(/at most 255/);
    expect(errors.description).toMatch(/at most 32000/);
    expect(validateAdo(form({ title: 'x'.repeat(255) })).title).toBeNull();
  });

  it('restricts priority to 1–4', () => {
    expect(validateAdo(form({ priority: '0' })).priority).toBeTruthy();
    expect(validateAdo(form({ priority: '5' })).priority).toBeTruthy();
    expect(validateAdo(form({ priority: '4' })).priority).toBeNull();
  });

  it('validates tags: max 10, no ";", allowed characters only', () => {
    expect(validateTag('needs;split')).toMatch(/;/);
    expect(validateTag('bad<tag>')).toMatch(/may only contain/);
    expect(validateTag('team:payments/eu-west 1.2')).toBeNull();
    expect(validateTag('équipe')).toBeNull();
    expect(validateTag('x'.repeat(65))).toMatch(/64/);
    const eleven = Array.from({ length: 11 }, (_, i) => `t${i}`);
    expect(validateAdo(form({ tags: eleven })).tags).toMatch(/At most 10/);
  });

  it('validates area/iteration path characters and length', () => {
    expect(validateAdo(form({ area_path: 'Payments\\Checkout' })).area_path).toBeNull();
    expect(validateAdo(form({ area_path: 'Bad|Path' })).area_path).toMatch(/not allowed/);
    expect(validateAdo(form({ iteration_path: 'Sprint: 1' })).iteration_path).toMatch(/not allowed/);
    expect(validateAdo(form({ iteration_path: 'a'.repeat(257) })).iteration_path).toMatch(/256/);
  });

  it('requires assigned_to to be an email address', () => {
    expect(validateAdo(form({ assigned_to: 'not an email' })).assigned_to).toBeTruthy();
    expect(validateAdo(form({ assigned_to: 'dev@contoso.com' })).assigned_to).toBeNull();
  });

  it('validates additional field reference names, duplicates and the 10-field limit', () => {
    const errors = validateAdo(
      form({
        additional_fields: [
          { name: 'Severity', value: '1', numeric: false },
          { name: 'Custom.A', value: 'x', numeric: false },
          { name: 'Custom.A', value: 'y', numeric: false },
          { name: 'Custom.B', value: 'v'.repeat(4001), numeric: false },
        ],
      }),
    );
    expect(errors['additional_fields.0.name']).toMatch(/reference name/);
    expect(errors['additional_fields.1.name']).toBeUndefined();
    expect(errors['additional_fields.2.name']).toMatch(/Duplicate/);
    expect(errors['additional_fields.3.value']).toMatch(/4000/);
    const many = Array.from({ length: 11 }, (_, i) => ({ name: `Custom.F${i}`, value: 'v', numeric: false }));
    expect(validateAdo(form({ additional_fields: many })).additional_fields).toMatch(/At most 10/);
  });

  it('builds the payload: trims, converts priority, nulls empty paths, keeps kind/source_email and numeric fields', () => {
    const payload = adoToPayload(
      form({
        title: '  Checkout fails  ',
        priority: '1',
        area_path: '   ',
        tags: [' a ', 'a', '', 'b'],
        assigned_to: '',
      }),
      base,
    );
    expect(payload).toMatchObject({
      kind: 'create_azure_devops_work_item',
      title: 'Checkout fails',
      priority: 1,
      area_path: null,
      assigned_to: null,
      tags: ['a', 'b'],
      source_email: base.source_email,
    });
    expect(payload.additional_fields).toEqual({
      'Microsoft.VSTS.Common.Severity': '2 - High',
      'Custom.Score': 7,
    });
  });
});

describe('reply / follow-up / no-action validation', () => {
  it('reply: subject 1–255 and body 1–20000, recipient preserved from the original payload', () => {
    expect(validateReply({ subject: '', body: '' })).toMatchObject({
      subject: expect.stringMatching(/required/) as unknown,
      body: expect.stringMatching(/required/) as unknown,
    });
    expect(validateReply({ subject: 's'.repeat(256), body: 'b'.repeat(20_001) })).toMatchObject({
      subject: expect.stringMatching(/255/) as unknown,
      body: expect.stringMatching(/20000/) as unknown,
    });
    const payload = replyToPayload(
      { subject: 'Re: hi', body: 'Thanks' },
      {
        to: ['sender@example.com'],
        source_email: { email_id: 'e' },
        in_reply_to_internet_message_id: '<id>',
      },
    );
    expect(payload.to).toEqual(['sender@example.com']);
    expect(payload.in_reply_to_internet_message_id).toBe('<id>');
  });

  it('follow-up: title required (≤255), description ≤10000, due date round-trips as ISO', () => {
    expect(validateFollowUp({ title: '', description: '', due_at: null }).title).toMatch(/required/);
    expect(
      validateFollowUp({ title: 't', description: 'd'.repeat(10_001), due_at: null }).description,
    ).toMatch(/10000/);
    const values = followUpToForm({ title: 'Call back', description: '', due_at: '2026-11-02T09:30:00Z' });
    const payload = followUpToPayload(values, { source_email: { email_id: 'e' } });
    expect(payload.due_at).toBe('2026-11-02T09:30:00.000Z');
    expect(followUpToPayload({ ...values, due_at: null }, {}).due_at).toBeNull();
  });

  it('no-action: reason 1–1000', () => {
    expect(validateNoAction({ reason: '  ' }).reason).toMatch(/required/);
    expect(validateNoAction({ reason: 'r'.repeat(1001) }).reason).toMatch(/1000/);
    expect(validateNoAction({ reason: 'Newsletter' }).reason).toBeNull();
  });
});

describe('mapServerFieldErrors', () => {
  it('maps known fields and collects the rest as general errors', () => {
    expect(
      mapServerFieldErrors(
        [
          { field: 'title', message: 'too long' },
          { field: 'tags.3', message: 'Invalid tag' },
          { field: 'source_email.email_id', message: 'bad' },
          { field: 'create_azure_devops_work_item.project', message: 'Project not allowed' },
        ],
        ['title', 'tags', 'project'],
      ),
    ).toEqual({
      fieldErrors: { title: 'too long', tags: 'Invalid tag', project: 'Project not allowed' },
      general: ['source_email.email_id: bad'],
    });
  });
});

describe('form model edge cases', () => {
  it('adoToForm tolerates missing / malformed fields', () => {
    expect(adoToForm({})).toEqual({
      project: '',
      work_item_type: '',
      title: '',
      description: '',
      priority: '2',
      area_path: '',
      iteration_path: '',
      tags: [],
      assigned_to: '',
      additional_fields: [],
    });
    expect(adoToForm({ priority: 'x' as unknown as number }).priority).toBe('2');
    expect(adoToForm({ tags: 'a,b' as unknown as string[] }).tags).toEqual([]);
    expect(adoToForm({ area_path: 'A\\B', assigned_to: 'dev@contoso.com', priority: 4 })).toMatchObject({
      area_path: 'A\\B',
      assigned_to: 'dev@contoso.com',
      priority: '4',
    });
  });

  it('validateTag / validatePath ignore blank values', () => {
    expect(validateTag('   ')).toBeNull();
    expect(validatePath('  ', 'Area path')).toBeNull();
    expect(validatePath('Bad\u0007Path', 'Area path')).toMatch(/not allowed/);
  });

  it('validateAdo: duplicate tags count once; the first invalid tag is reported', () => {
    const ten = Array.from({ length: 10 }, (_, i) => `t${i}`);
    expect(validateAdo(form({ tags: [...ten, 't0', ' t1 '] })).tags).toBeNull();
    expect(validateAdo(form({ tags: ['ok', 'bad<1>', 'semi;colon'] })).tags).toMatch(/bad<1>/);
  });

  it('validateAdo: a value without a field name is an error; fully empty rows are ignored', () => {
    const errors = validateAdo(
      form({
        additional_fields: [
          { name: '', value: '', numeric: false },
          { name: '  ', value: 'orphan', numeric: false },
        ],
      }),
    );
    expect(errors['additional_fields.0.name']).toBeUndefined();
    expect(errors['additional_fields.1.name']).toBe('Field reference name is required');
    expect(errors.additional_fields).toBeUndefined();
    expect(validateAdo(form({ assigned_to: '   ' })).assigned_to).toBeNull();
  });

  it('adoToPayload: numeric rows keep strings that no longer parse; nameless rows are dropped', () => {
    const payload = adoToPayload(
      form({
        additional_fields: [
          { name: 'Custom.Score', value: 'n/a', numeric: true },
          { name: 'Custom.Empty', value: ' ', numeric: true },
          { name: 'Custom.Count', value: ' 12 ', numeric: true },
          { name: '', value: 'dropped', numeric: false },
          { name: ' Custom.Text ', value: '42', numeric: false },
        ],
      }),
      { ...base, source_email: undefined },
    );
    expect(payload.additional_fields).toEqual({
      'Custom.Score': 'n/a',
      'Custom.Empty': ' ',
      'Custom.Count': 12,
      'Custom.Text': '42',
    });
    expect(payload.source_email).toEqual({ email_id: '' });
  });

  it('reply: defaults for a partial payload', () => {
    expect(replyToForm({})).toEqual({ subject: '', body: '' });
    expect(replyToPayload({ subject: ' S ', body: ' B ' }, {})).toEqual({
      kind: 'propose_email_reply',
      to: [],
      subject: 'S',
      body: 'B',
      in_reply_to_internet_message_id: null,
      source_email: { email_id: '' },
    });
  });

  it('follow-up: invalid or missing due dates', () => {
    expect(followUpToForm({}).due_at).toBeNull();
    expect(followUpToForm({ due_at: 'not a date' }).due_at).toBeNull();
    expect(validateFollowUp({ title: 't', description: '', due_at: 'garbage' }).due_at).toBe(
      'Due date is not a valid date',
    );
    expect(
      validateFollowUp({ title: 't', description: '', due_at: '2026-11-02 09:30:00' }).due_at,
    ).toBeNull();
    expect(followUpToPayload({ title: ' T ', description: ' D ', due_at: null }, {})).toEqual({
      kind: 'create_follow_up_task',
      title: 'T',
      description: 'D',
      due_at: null,
      source_email: { email_id: '' },
    });
  });

  it('no-action: form + payload', () => {
    expect(noActionToForm({})).toEqual({ reason: '' });
    expect(noActionToForm({ reason: 'Newsletter' })).toEqual({ reason: 'Newsletter' });
    expect(noActionToPayload({ reason: '  Newsletter ' }, { source_email: { email_id: 'e' } })).toEqual({
      kind: 'mark_no_action',
      reason: 'Newsletter',
      source_email: { email_id: 'e' },
    });
    expect(noActionToPayload({ reason: 'x' }, {}).source_email).toEqual({ email_id: '' });
  });

  it('hasErrors / compactErrors', () => {
    expect(hasErrors({ a: null, b: null })).toBe(false);
    expect(hasErrors({ a: null, b: 'bad' })).toBe(true);
    expect(compactErrors({ a: null, b: 'bad', c: '' })).toEqual({ b: 'bad' });
  });

  it('mapServerFieldErrors joins repeated messages and keeps field-less messages as general errors', () => {
    expect(
      mapServerFieldErrors(
        [
          { field: 'title', message: 'too long' },
          { field: 'title', message: 'contains a URL' },
          { field: '', message: 'Payload rejected by policy' },
          { field: 'propose_email_reply', message: 'Unknown kind' },
        ],
        ['title'],
      ),
    ).toEqual({
      fieldErrors: { title: 'too long; contains a URL' },
      general: ['Payload rejected by policy', 'propose_email_reply: Unknown kind'],
    });
  });
});
