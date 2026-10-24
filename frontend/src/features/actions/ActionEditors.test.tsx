import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiError } from '../../api/client';
import type { ActionDetail, ActionType } from '../../api/types';
import { dayjs } from '../../lib/format';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { HASH_V4, adoAction, followUpAction, noActionAction, replyAction } from '../../test/fixtures';
import {
  callsTo,
  deferred,
  errorResponse,
  jsonResponse,
  mockFetch,
  renderWithProviders,
  type Route,
} from '../../test/utils';
import { ActionEditor, type EditorProps } from './ActionEditors';

const PROJECTS = '/api/v1/integrations/azure-devops/projects';
const TYPES = (project: string) => `${PROJECTS}/${encodeURIComponent(project)}/work-item-types`;
const VALIDATE = '/api/v1/integrations/azure-devops/validate';

const ADO_ROUTES: Route[] = [
  {
    method: 'GET',
    path: PROJECTS,
    handler: () =>
      jsonResponse({
        items: [
          { id: 'p1', name: 'Payments', state: 'wellFormed', url: null },
          { id: 'p2', name: 'Ops', state: 'wellFormed', url: null },
        ],
      }),
  },
  {
    method: 'GET',
    path: /\/work-item-types$/,
    handler: (call) =>
      jsonResponse({
        project: decodeURIComponent(call.url.split('/projects/')[1]!.split('/')[0]!),
        items: [
          {
            name: 'Bug',
            reference_name: 'Microsoft.VSTS.WorkItemTypes.Bug',
            description: null,
            is_disabled: false,
          },
          { name: 'Task', reference_name: null, description: null, is_disabled: false },
          { name: 'Epic', reference_name: null, description: null, is_disabled: true },
        ],
      }),
  },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderEditor(action: ActionDetail, props: Partial<EditorProps> = {}, routes: Route[] = []) {
  const handlers = { onSaved: vi.fn(), onConflict: vi.fn(), onQueued: vi.fn() };
  const fetch = mockFetch([...routes, ...ADO_ROUTES]);
  const view = renderWithProviders(
    <ActionEditor action={action} editable queued={false} {...handlers} {...props} />,
    { withNotifications: true },
  );
  return { ...view, ...handlers, ...fetch };
}

function patchRoute(handler: Route['handler']): Route {
  return { method: 'PATCH', path: `/api/v1/actions/${'act-1'}`, handler };
}

const saveButton = () => screen.getByRole('button', { name: 'Save changes' });

/** Faster than typing char by char (each keystroke re-renders the whole controlled form). */
async function fill(user: ReturnType<typeof userEvent.setup>, element: HTMLElement, text: string) {
  await user.click(element);
  await user.paste(text);
}

describe('<AdoEditor>', () => {
  it('renders the payload in editable fields with counters, and Save stays disabled until edited', async () => {
    renderEditor(adoAction());
    expect(
      screen.getByText('Edit the payload before approving. Saving creates a new version.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: /^Project/ })).toHaveValue('Payments');
    expect(screen.getByRole('combobox', { name: /^Work item type/ })).toHaveValue('Bug');
    const title = screen.getByRole('textbox', { name: /^Title/ });
    expect(title).toHaveValue('Checkout fails with 500 for EU customers');
    expect(title).toHaveAccessibleDescription('40/255');
    const description = 'Customers in the EU region see HTTP 500 on checkout.';
    expect(screen.getByRole('textbox', { name: 'Description' })).toHaveAccessibleDescription(
      `${description.length}/32,000`,
    );
    expect(screen.getByRole('combobox', { name: /^Priority/ })).toHaveValue('1 — Highest');
    expect(screen.getByText('checkout')).toBeInTheDocument();
    expect(
      screen.getByText('None. Only fields allowed by tenant settings are accepted.'),
    ).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
    await expectNoSeriousA11yViolations();
  });

  it('offers projects from the API and work-item types only for a listed project (disabled types hidden)', async () => {
    const user = userEvent.setup();
    const { calls } = renderEditor(adoAction());
    await waitFor(() => expect(callsTo(calls, 'GET', TYPES('Payments'))).toHaveLength(1));

    await user.click(screen.getByRole('combobox', { name: /^Work item type/ }));
    await user.clear(screen.getByRole('combobox', { name: /^Work item type/ }));
    const typeOptions = await screen.findAllByRole('option');
    expect(typeOptions.map((o) => o.textContent)).toEqual(['Bug', 'Task']);

    const project = screen.getByRole('combobox', { name: /^Project/ });
    await user.clear(project);
    const projectOptions = await screen.findAllByRole('option');
    expect(projectOptions.map((o) => o.textContent)).toEqual(['Payments', 'Ops']);
    await user.click(screen.getByRole('option', { name: 'Ops' }));
    expect(project).toHaveValue('Ops');
    await waitFor(() => expect(callsTo(calls, 'GET', TYPES('Ops'))).toHaveLength(1));

    await user.clear(project);
    await user.type(project, 'Unlisted');
    await user.tab();
    expect(callsTo(calls, 'GET', TYPES('Unlisted'))).toHaveLength(0);
  });

  it('validates on blur and blocks saving invalid values (no request)', async () => {
    const user = userEvent.setup();
    const { calls } = renderEditor(adoAction());
    const title = screen.getByRole('textbox', { name: /^Title/ });
    await user.clear(title);
    await user.tab();
    expect(title).toHaveAttribute('aria-invalid', 'true');
    expect(title).toHaveAccessibleDescription(expect.stringContaining('Title is required'));

    const assignee = screen.getByRole('textbox', { name: 'Assigned to' });
    await fill(user, assignee, 'not an email');
    await user.tab();
    expect(assignee).toHaveAccessibleDescription(
      expect.stringContaining('Assigned to must be an email address'),
    );

    await user.click(saveButton());
    expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')).toHaveLength(0);
  });

  it('ignores duplicate tags', async () => {
    const user = userEvent.setup();
    renderEditor(adoAction());
    await user.type(screen.getByRole('combobox', { name: 'Tags' }), 'checkout{Enter}');
    expect(screen.getAllByText('checkout')).toHaveLength(1);
  });

  it('shows tag validation errors inline', async () => {
    const user = userEvent.setup();
    renderEditor(adoAction());
    const tags = screen.getByRole('combobox', { name: 'Tags' });
    await user.type(tags, 'bad<tag>{Enter}');
    expect(tags).toHaveAccessibleDescription(
      expect.stringContaining('Tag “bad<tag>” may only contain letters, digits, spaces and _ . - : /'),
    );
  });

  it('adds, validates and removes additional fields; the 10-field limit disables "Add field"', async () => {
    const user = userEvent.setup();
    const { unmount } = renderEditor(adoAction());
    await user.click(screen.getByRole('button', { name: 'Add field' }));
    const name = screen.getByRole('textbox', { name: 'Additional field 1 reference name' });
    await fill(user, name, 'Severity');
    await user.type(screen.getByRole('textbox', { name: 'Additional field 1 value' }), '2');
    await user.tab();
    expect(name).toHaveAccessibleDescription(
      expect.stringContaining('Use a field reference name, e.g. Microsoft.VSTS.Common.Severity'),
    );
    await user.click(screen.getByRole('button', { name: 'Remove additional field 1' }));
    expect(screen.queryByRole('textbox', { name: 'Additional field 1 reference name' })).toBeNull();
    unmount();

    const ten = Object.fromEntries(Array.from({ length: 10 }, (_, i) => [`Custom.F${i}`, i]));
    renderEditor(adoAction({ payload: { ...adoAction().payload, additional_fields: ten } }));
    expect(screen.getByRole('button', { name: 'Add field' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: 'Additional field 10 value' })).toHaveValue('9');
  });

  it('PATCHes expected_version + the trimmed payload (numeric additional fields stay numbers)', async () => {
    const user = userEvent.setup();
    const saved = adoAction({ version: 4, payload_hash: HASH_V4 });
    const action = adoAction({
      payload: { ...adoAction().payload, additional_fields: { 'Custom.Score': 7, 'Custom.Label': 'x' } },
    });
    const { calls, onSaved } = renderEditor(action, {}, [patchRoute(() => jsonResponse(saved))]);
    const title = screen.getByRole('textbox', { name: /^Title/ });
    await user.clear(title);
    await fill(user, title, '  Checkout returns HTTP 500 (EU)  ');
    await fill(user, screen.getByRole('textbox', { name: 'Area path' }), 'Payments\\Checkout');
    await user.click(saveButton());
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(action, saved));
    const body = callsTo(calls, 'PATCH', '/api/v1/actions/act-1')[0]!.body as {
      expected_version: number;
      payload: Record<string, unknown>;
    };
    expect(body.expected_version).toBe(3);
    expect(body.payload).toEqual({
      kind: 'create_azure_devops_work_item',
      project: 'Payments',
      work_item_type: 'Bug',
      title: 'Checkout returns HTTP 500 (EU)',
      description: 'Customers in the EU region see HTTP 500 on checkout.',
      priority: 1,
      area_path: 'Payments\\Checkout',
      iteration_path: null,
      tags: ['checkout'],
      assigned_to: null,
      additional_fields: { 'Custom.Score': 7, 'Custom.Label': 'x' },
      source_email: { email_id: 'email-1', subject: 'URGENT: checkout broken' },
    });
  });

  it('regression: a double click on Save sends ONE PATCH for the loaded expected_version', async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    const { calls } = renderEditor(adoAction(), {}, [patchRoute(() => pending.promise)]);
    await user.type(screen.getByRole('textbox', { name: /^Title/ }), '!');
    fireEvent.click(saveButton());
    fireEvent.click(saveButton());
    await waitFor(() => expect(saveButton()).toHaveAttribute('data-loading'));
    expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')).toHaveLength(1);
    // Every decision is blocked while saving.
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
    pending.resolve(errorResponse(500, 'internal_error'));
    expect(await screen.findByText('Could not save changes')).toBeInTheDocument();
    // The guard is released: the next deliberate save goes out.
    await user.click(saveButton());
    await waitFor(() => expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')).toHaveLength(2));
  });

  it('maps 422 field errors onto inputs (envelope + kind prefixes stripped) and lists the rest', async () => {
    const user = userEvent.setup();
    renderEditor(adoAction(), {}, [
      patchRoute(() =>
        errorResponse(422, 'validation_failed', {
          message: 'The request failed validation.',
          details: [
            {
              loc: ['body', 'payload', 'create_azure_devops_work_item', 'title'],
              msg: 'String should have at most 255 characters',
            },
            { field: 'payload.project', message: 'Project "Ops" is not allowed' },
            { field: 'payload.project', message: 'Project is archived' },
            { field: 'payload.tags.0', message: 'Invalid tag' },
            { field: 'payload.source_email.email_id', message: 'Unknown email' },
          ],
        }),
      ),
    ]);
    await user.type(screen.getByRole('textbox', { name: /^Title/ }), '!');
    await user.click(saveButton());
    const alert = await screen.findByRole('alert', { name: 'The server rejected these changes' });
    expect(
      within(alert)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['source_email.email_id: Unknown email']);
    const title = screen.getByRole('textbox', { name: /^Title/ });
    expect(title).toHaveAttribute('aria-invalid', 'true');
    expect(title).toHaveAccessibleDescription(
      expect.stringContaining('String should have at most 255 characters'),
    );
    expect(screen.getByRole('combobox', { name: /^Project/ })).toHaveAccessibleDescription(
      expect.stringContaining('Project "Ops" is not allowed; Project is archived'),
    );
    expect(screen.getByRole('combobox', { name: 'Tags' })).toHaveAccessibleDescription(
      expect.stringContaining('Invalid tag'),
    );
  });

  it('a 422 that only targets fields shows no general alert; one without details shows the message', async () => {
    const user = userEvent.setup();
    let details: Record<string, unknown>[] = [{ field: 'payload.title', message: 'Too vague' }];
    renderEditor(adoAction(), {}, [
      patchRoute(() =>
        errorResponse(422, 'validation_failed', { message: 'The request failed validation.', details }),
      ),
    ]);
    await user.type(screen.getByRole('textbox', { name: /^Title/ }), '!');
    await user.click(saveButton());
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: /^Title/ })).toHaveAccessibleDescription(
        expect.stringContaining('Too vague'),
      ),
    );
    expect(screen.queryByRole('alert', { name: 'The server rejected these changes' })).toBeNull();

    details = [];
    await user.click(saveButton());
    const alert = await screen.findByRole('alert', { name: 'The server rejected these changes' });
    expect(alert).toHaveTextContent('The request failed validation.');

    // Discard resets the form and clears server errors.
    await user.click(screen.getByRole('button', { name: 'Discard' }));
    expect(screen.queryByRole('alert', { name: 'The server rejected these changes' })).toBeNull();
    expect(screen.getByRole('textbox', { name: /^Title/ })).toHaveValue(
      'Checkout fails with 500 for EU customers',
    );
    expect(saveButton()).toBeDisabled();
  });

  it('409 stale_version is reported to the card; other errors toast with the support reference', async () => {
    const user = userEvent.setup();
    let status = 409;
    const { onConflict, onSaved } = renderEditor(adoAction(), {}, [
      patchRoute(() =>
        status === 409
          ? errorResponse(409, 'stale_version', { correlationId: 'corr-stale' })
          : errorResponse(500, 'internal_error', {
              message: 'Database unavailable',
              correlationId: 'corr-500',
            }),
      ),
    ]);
    await user.type(screen.getByRole('textbox', { name: /^Title/ }), '!');
    await user.click(saveButton());
    await waitFor(() => expect(onConflict).toHaveBeenCalledTimes(1));
    expect((onConflict.mock.calls[0]![0] as ApiError).supportReference).toBe('corr-stale');

    status = 500;
    await user.click(saveButton());
    expect(await screen.findByText('Could not save changes')).toBeInTheDocument();
    expect(screen.getByText('Database unavailable')).toBeInTheDocument();
    expect(screen.getByText('internal_error · ref corr-500')).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('"Validate with Azure DevOps" posts the current payload with server_side=true and shows the result', async () => {
    const user = userEvent.setup();
    let result: Record<string, unknown> = {
      valid: true,
      schema_issues: [],
      policy_issues: [],
      server_issues: [],
      server_validated: true,
    };
    const { calls } = renderEditor(adoAction(), {}, [
      { method: 'POST', path: VALIDATE, handler: () => jsonResponse(result) },
    ]);
    const title = screen.getByRole('textbox', { name: /^Title/ });
    await user.clear(title);
    await fill(user, title, 'Edited title');
    await user.click(screen.getByRole('button', { name: 'Validate with Azure DevOps' }));
    const ok = await screen.findByRole('status', { name: 'Payload is valid' });
    expect(ok).toHaveTextContent('Validated by Azure DevOps (validateOnly — nothing was created).');
    expect(callsTo(calls, 'POST', VALIDATE)[0]!.body).toMatchObject({
      server_side: true,
      payload: { kind: 'create_azure_devops_work_item', title: 'Edited title', project: 'Payments' },
    });
    // Success toast too.
    expect(screen.getAllByText('Payload is valid').length).toBeGreaterThanOrEqual(2);

    result = {
      valid: false,
      schema_issues: [{ field: 'title', message: 'too long' }],
      policy_issues: ['Project "Payments" requires an area path'],
      server_issues: [{ field: null, message: 'TF401320: Rule error' }],
      server_validated: false,
    };
    await user.click(screen.getByRole('button', { name: 'Validate with Azure DevOps' }));
    const bad = await screen.findByRole('status', { name: 'Payload has issues' });
    expect(bad).toHaveTextContent('Validated against the schema and tenant policy (no server-side check).');
    expect(
      within(bad)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['title: too long', 'Project "Payments" requires an area path', 'TF401320: Rule error']);
  });

  it('a failing validation request shows an error toast', async () => {
    const user = userEvent.setup();
    renderEditor(adoAction(), {}, [
      { method: 'POST', path: VALIDATE, handler: () => errorResponse(502, 'integration_error') },
    ]);
    await user.click(screen.getByRole('button', { name: 'Validate with Azure DevOps' }));
    expect(await screen.findByText('Validation request failed')).toBeInTheDocument();
    expect(screen.getByText('integration_error · ref corr-integration_error')).toBeInTheDocument();
  });

  it('read-only: inputs disabled, no project lookup, no validate button', async () => {
    const { calls } = renderEditor(adoAction(), { editable: false });
    expect(screen.getByText('Payload (read-only)')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: /^Title/ })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: /^Project/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Add field' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Validate with Azure DevOps' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull();
    await new Promise((r) => setTimeout(r, 20));
    expect(callsTo(calls, 'GET', PROJECTS)).toHaveLength(0);
  });

  it('tolerates a payload with missing fields (defaults, priority 2)', () => {
    renderEditor(adoAction({ payload: { kind: 'create_azure_devops_work_item', priority: 9 } }));
    expect(screen.getByRole('textbox', { name: /^Title/ })).toHaveValue('');
    expect(screen.getByRole('combobox', { name: /^Priority/ })).toHaveValue('2 — High');
  });
});

describe('<ReplyEditor>', () => {
  it('shows the never-sent notice, a read-only recipient and counters', async () => {
    renderEditor(replyAction());
    expect(
      screen.getByText(
        'Drafts are never sent by NUVEXA. Approving stores the draft for you to copy and send yourself.',
      ),
    ).toBeInTheDocument();
    const to = screen.getByRole('textbox', { name: 'To' });
    expect(to).toHaveValue('dana@fabrikam.example');
    expect(to).toBeDisabled();
    expect(to).toHaveAttribute('readonly');
    expect(screen.getByRole('textbox', { name: /^Subject/ })).toHaveAccessibleDescription('27/255');
    expect(screen.getByRole('textbox', { name: /^Body/ })).toHaveAccessibleDescription(
      `${replyAction().payload.body as string}`.length.toString() + '/20,000',
    );
    await expectNoSeriousA11yViolations();
  });

  it('saves the trimmed subject/body and keeps the recipient + thread id from the original', async () => {
    const user = userEvent.setup();
    const { calls } = renderEditor(replyAction({ id: 'act-1' }), {}, [
      patchRoute(() => jsonResponse(replyAction({ id: 'act-1', version: 4 }))),
    ]);
    const subject = screen.getByRole('textbox', { name: /^Subject/ });
    await user.clear(subject);
    await fill(user, subject, '  Re: checkout  ');
    await user.click(saveButton());
    await waitFor(() => expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')).toHaveLength(1));
    expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')[0]!.body).toMatchObject({
      expected_version: 3,
      payload: {
        kind: 'propose_email_reply',
        to: ['dana@fabrikam.example'],
        subject: 'Re: checkout',
        in_reply_to_internet_message_id: '<abc@fabrikam.example>',
      },
    });
  });

  it('validates required subject/body and maps 422 errors onto them', async () => {
    const user = userEvent.setup();
    renderEditor(replyAction({ id: 'act-1' }), {}, [
      patchRoute(() =>
        errorResponse(422, 'validation_failed', {
          details: [{ loc: ['body', 'payload', 'propose_email_reply', 'body'], msg: 'Body contains a URL' }],
        }),
      ),
    ]);
    const subject = screen.getByRole('textbox', { name: /^Subject/ });
    await user.clear(subject);
    await user.tab();
    expect(subject).toHaveAccessibleDescription(expect.stringContaining('Subject is required'));
    await user.type(subject, 'Re: hi');
    await user.click(saveButton());
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: /^Body/ })).toHaveAccessibleDescription(
        expect.stringContaining('Body contains a URL'),
      ),
    );
  });
});

describe('<FollowUpEditor>', () => {
  it('renders title, description and the due date; the picker controls are named', async () => {
    const user = userEvent.setup();
    renderEditor(followUpAction());
    expect(screen.getByRole('textbox', { name: /^Title/ })).toHaveValue('Call Dana back');
    const due = screen.getByRole('button', { name: 'Due date' });
    expect(due).toHaveTextContent(dayjs('2026-11-02T09:30:00Z').format('MMM D, YYYY HH:mm'));
    expect(screen.getByRole('button', { name: 'Clear due date' })).toBeInTheDocument();
    await user.click(due);
    expect(screen.getByRole('button', { name: 'Previous month' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply due date' })).toBeInTheDocument();
    // Regression: the picker dropdown was an unnamed dialog and its icon buttons had no names.
    await expectNoSeriousA11yViolations();
  });

  it('clearing the due date saves null', async () => {
    const user = userEvent.setup();
    const { calls } = renderEditor(followUpAction({ id: 'act-1' }), {}, [
      patchRoute(() => jsonResponse(followUpAction({ id: 'act-1', version: 4 }))),
    ]);
    await user.click(screen.getByRole('button', { name: 'Clear due date' }));
    expect(screen.getByRole('button', { name: 'Due date' })).toHaveTextContent('No due date');
    await user.click(saveButton());
    await waitFor(() => expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')).toHaveLength(1));
    expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')[0]!.body).toMatchObject({
      payload: { kind: 'create_follow_up_task', title: 'Call Dana back', due_at: null },
    });
  });

  it('picking a new day keeps the time and sends ISO-8601 (UTC)', async () => {
    const user = userEvent.setup();
    const { calls } = renderEditor(followUpAction({ id: 'act-1' }), {}, [
      patchRoute(() => jsonResponse(followUpAction({ id: 'act-1', version: 4 }))),
    ]);
    await user.click(screen.getByRole('button', { name: 'Due date' }));
    await user.click(screen.getByRole('button', { name: '15 November 2026' }));
    await user.click(screen.getByRole('button', { name: 'Apply due date' }));
    await user.click(saveButton());
    await waitFor(() => expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')).toHaveLength(1));
    const expected = dayjs('2026-11-02T09:30:00Z').date(15).toISOString();
    expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')[0]!.body).toMatchObject({
      payload: { due_at: expected },
    });
  });

  it('maps a 422 on due_at onto the picker', async () => {
    const user = userEvent.setup();
    renderEditor(followUpAction({ id: 'act-1' }), {}, [
      patchRoute(() =>
        errorResponse(422, 'validation_failed', {
          details: [
            { field: 'payload.create_follow_up_task.due_at', message: 'Due date must be in the future' },
          ],
        }),
      ),
    ]);
    await user.type(screen.getByRole('textbox', { name: /^Title/ }), '!');
    await user.click(saveButton());
    expect(await screen.findByText('Due date must be in the future')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Due date' })).toHaveAttribute('aria-invalid', 'true');
  });
});

describe('<NoActionEditor>', () => {
  it('edits the reason, validates it and maps a 422 onto it', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = renderEditor(noActionAction({ id: 'act-1' }), {}, [
      patchRoute(() =>
        fail
          ? errorResponse(422, 'validation_failed', {
              details: [{ field: 'payload.mark_no_action.reason', message: 'Reason is too generic' }],
            })
          : jsonResponse(noActionAction({ id: 'act-1', version: 4 })),
      ),
    ]);
    const reason = screen.getByRole('textbox', { name: /^Reason/ });
    expect(reason).toHaveValue('Marketing newsletter, informational only.');
    await user.clear(reason);
    await user.tab();
    expect(reason).toHaveAccessibleDescription(expect.stringContaining('Reason is required'));
    await user.type(reason, 'Spam');
    await user.click(saveButton());
    await waitFor(() =>
      expect(reason).toHaveAccessibleDescription(expect.stringContaining('Reason is too generic')),
    );
    fail = false;
    await user.click(saveButton());
    await waitFor(() => expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')).toHaveLength(2));
    expect(callsTo(calls, 'PATCH', '/api/v1/actions/act-1')[1]!.body).toMatchObject({
      payload: { kind: 'mark_no_action', reason: 'Spam', source_email: { email_id: 'email-1' } },
    });
  });

  it('is read-only when not editable', () => {
    renderEditor(noActionAction(), { editable: false });
    expect(screen.getByRole('textbox', { name: /^Reason/ })).toBeDisabled();
    expect(screen.getByText('Payload (read-only)')).toBeInTheDocument();
  });
});

describe('<ActionEditor>', () => {
  it('falls back for an unsupported action type', () => {
    renderEditor(adoAction({ action_type: 'launch_rocket' as ActionType }));
    expect(screen.getByText('Unsupported action type.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull();
  });
});
