import { screen, waitFor, within } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EMAIL_CATEGORIES, type MeOut, type SettingsOut, type TenantSettings } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { PERSONAS, settingsOut } from '../../test/fixtures';
import { callsTo, errorResponse, jsonResponse, mockFetch, renderWithProviders } from '../../test/utils';
import { SettingsPage } from './SettingsPage';

const SETTINGS = '/api/v1/settings';

function setup({
  get = () => settingsOut(),
  put = (body: { expected_version: number; settings: TenantSettings }) =>
    settingsOut({ version: body.expected_version + 1, settings: body.settings }),
  session = PERSONAS.admin,
}: {
  get?: () => SettingsOut | Response;
  put?: (body: { expected_version: number; settings: TenantSettings }) => SettingsOut | Response;
  session?: MeOut;
} = {}) {
  const { calls } = mockFetch([
    {
      method: 'GET',
      path: SETTINGS,
      handler: () => {
        const value = get();
        return value instanceof Response ? value : jsonResponse(value);
      },
    },
    {
      method: 'PUT',
      path: SETTINGS,
      handler: (call) => {
        const value = put(call.body as { expected_version: number; settings: TenantSettings });
        return value instanceof Response ? value : jsonResponse(value);
      },
    },
  ]);
  const result = renderWithProviders(<SettingsPage />, { session, withNotifications: true });
  return { ...result, calls };
}

async function chooseOption(user: UserEvent, comboboxName: string, option: string) {
  await user.click(screen.getByRole('combobox', { name: comboboxName }));
  await user.click(await screen.findByRole('option', { name: option }));
}

function categoryPanel(label: string): HTMLElement {
  return screen
    .getByRole('textbox', { name: `Description for ${label}` })
    .closest('.mantine-Paper-root') as HTMLElement;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('<SettingsPage> states', () => {
  it('shows a skeleton while loading', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => undefined)),
    );
    renderWithProviders(<SettingsPage />, { session: PERSONAS.admin });
    expect(screen.getByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument();
    expect(screen.getByLabelText('Loading settings')).toHaveAttribute('aria-busy', 'true');
  });

  it('shows the error with its support reference and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = setup({
      get: () =>
        fail ? errorResponse(500, 'internal_error', { correlationId: 'corr-settings' }) : settingsOut(),
    });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('corr-settings');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('textbox', { name: 'Default project' })).toHaveValue('Payments');
    expect(callsTo(calls, 'GET', SETTINGS)).toHaveLength(2);
  });
});

describe('<SettingsPage> read-only for non-admins', () => {
  it.each(['viewer', 'approver'] as const)('is fully read-only for a %s', async (role) => {
    const user = userEvent.setup();
    const { calls } = setup({ session: PERSONAS[role] });
    expect(await screen.findByText('Read-only: only tenant admins can change settings.')).toBeInTheDocument();
    const project = await screen.findByRole('textbox', { name: 'Default project' });
    // Every text input, select, number input, tags input, switch and textarea.
    const controls = project.closest('form')!.querySelectorAll('input:not([type="hidden"]), textarea');
    expect(controls.length).toBeGreaterThan(50);
    for (const control of controls) expect(control).toBeDisabled();
    await user.type(project, 'Hacked');
    await user.click(within(categoryPanel('Follow-up')).getByRole('switch', { name: 'Enabled' }));
    expect(project).toHaveValue('Payments');
    expect(screen.queryByText('Unsaved changes')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save settings' })).toBeNull();
    expect(callsTo(calls, 'PUT', SETTINGS)).toHaveLength(0);
  });
});

describe('<SettingsPage> admin editing', () => {
  it('renders the loaded settings with the version; no accessibility violations', async () => {
    setup();
    expect(await screen.findByRole('textbox', { name: 'Default project' })).toHaveValue('Payments');
    expect(screen.getByText('Version 2')).toBeInTheDocument();
    expect(screen.getByText(/^Updated/)).toBeInTheDocument();
    expect(screen.queryByText('Read-only: only tenant admins can change settings.')).toBeNull();
    expect(screen.getByRole('textbox', { name: 'Default work item type' })).toHaveValue('Task');
    expect(screen.getByRole('combobox', { name: 'Work item type for Incident' })).toHaveValue('Bug');
    expect(screen.getByRole('combobox', { name: 'Work item type for Follow-up' })).toHaveValue('');
    expect(screen.getByRole('combobox', { name: 'Default priority for Incident' })).toHaveValue('High');
    expect(screen.getByRole('combobox', { name: 'Default priority for Bug' })).toHaveValue('Low');
    expect(screen.getByRole('combobox', { name: 'Critical' })).toHaveValue('1 — Highest');
    expect(screen.getByRole('textbox', { name: 'Description for Incident' })).toHaveValue(
      'Production outage.',
    );
    expect(screen.getByRole('textbox', { name: 'High ≥' })).toHaveValue('0.75');
    expect(screen.getByRole('textbox', { name: 'Audit events (days)' })).toHaveValue('365');
    expect(within(categoryPanel('Incident')).getByRole('switch', { name: 'Enabled' })).toBeChecked();
    expect(within(categoryPanel('Follow-up')).getByRole('switch', { name: 'Enabled' })).not.toBeChecked();
    // "Other" is the fallback and attachment processing is unavailable: both locked.
    const other = within(categoryPanel('Other')).getByRole('switch', { name: 'Always enabled' });
    expect(other).toBeChecked();
    expect(other).toBeDisabled();
    const attachments = screen.getByRole('switch', { name: /^Attachment processing/ });
    expect(attachments).not.toBeChecked();
    expect(attachments).toBeDisabled();
    expect(screen.queryByText('Unsaved changes')).toBeNull();

    await expectNoSeriousA11yViolations();
  });

  it('shows the save bar once edited and discards back to the loaded values', async () => {
    const user = userEvent.setup();
    setup();
    const project = await screen.findByRole('textbox', { name: 'Default project' });
    await user.type(project, ' EU');
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Discard' }));
    expect(project).toHaveValue('Payments');
    expect(screen.queryByText('Unsaved changes')).toBeNull();
  });

  // The form has ~100 controlled inputs and every interaction re-renders all of them: paste instead
  // of typing, no inter-action delay, and keep each test to a handful of interactions.
  const fastUser = () => userEvent.setup({ delay: null });
  const fill = async (user: UserEvent, label: string, value: string) => {
    const input = screen.getByLabelText(label);
    await user.clear(input);
    await user.click(input);
    await user.paste(value);
  };

  it('PUTs the exact transformed settings with expected_version and shows the confirmed version', async () => {
    const user = fastUser();
    const { calls } = setup();
    await screen.findByLabelText('Default project');
    await fill(user, 'Default project', '  Checkout ');
    await fill(user, 'Default area path', 'Payments\\EU');
    await user.type(screen.getByRole('combobox', { name: 'Allowed projects' }), 'Ops{Enter}');
    await user.click(within(categoryPanel('Follow-up')).getByRole('switch', { name: 'Enabled' }));
    await user.click(within(categoryPanel('Incident')).getByRole('switch', { name: 'Enabled' }));
    await fill(user, 'Description for Follow-up', 'Chasers');
    await fill(user, 'High ≥', '0.8');
    await fill(user, 'Audit events (days)', '400');
    await user.click(screen.getByRole('switch', { name: /Auto-analyze new emails/ }));

    await user.click(screen.getByRole('button', { name: 'Save settings' }));

    expect(await screen.findByText('Settings saved')).toBeInTheDocument();
    expect(screen.getByText('Tenant settings are now at version 3.')).toBeInTheDocument();
    const put = callsTo(calls, 'PUT', SETTINGS);
    expect(put).toHaveLength(1);
    expect(put[0]!.body).toEqual({
      expected_version: 2,
      settings: {
        azure_devops: {
          project: 'Checkout',
          work_item_type: 'Task',
          area_path: 'Payments\\EU',
          iteration_path: null,
          allowed_projects: ['Ops'],
          allowed_work_item_types: ['Bug', 'Task', 'User Story'],
          allowed_additional_fields: ['Microsoft.VSTS.Common.Severity'],
          category_work_item_types: { incident: 'Bug', bug: 'Bug' },
        },
        taxonomy: {
          enabled_categories: ['bug', 'follow_up', 'other'],
          category_descriptions: { incident: 'Production outage.', follow_up: 'Chasers' },
        },
        priority: {
          category_default_priority: Object.fromEntries(
            EMAIL_CATEGORIES.map((c) => [c, c === 'incident' ? 'high' : 'low']),
          ),
          azure_devops_priority: { critical: 1, high: 1, medium: 2, low: 3 },
        },
        confidence: { high: 0.8, medium: 0.5 },
        retention: { email_body_days: 90, workflow_event_days: 180, audit_days: 400 },
        auto_analyze_new_emails: true,
        attachment_processing_enabled: false,
      },
    });
    // The confirmed server state replaces the form (remounted at the new version).
    expect(await screen.findByText('Version 3')).toBeInTheDocument();
    expect(screen.queryByText('Unsaved changes')).toBeNull();
    expect(screen.getByRole('textbox', { name: 'Default project' })).toHaveValue('Checkout');
    expect(callsTo(calls, 'GET', SETTINGS)).toHaveLength(1);
  });

  it('maps the category type, default priority and Azure DevOps priority selects into the PUT body', async () => {
    const user = fastUser();
    const { calls } = setup();
    await screen.findByLabelText('Default project');
    await chooseOption(user, 'Work item type for Feature request', 'User Story');
    await chooseOption(user, 'Default priority for Bug', 'Critical');
    await chooseOption(user, 'Low', '4 — Low');

    await user.click(screen.getByRole('button', { name: 'Save settings' }));

    expect(await screen.findByText('Settings saved')).toBeInTheDocument();
    const put = callsTo(calls, 'PUT', SETTINGS);
    expect(put).toHaveLength(1);
    expect(put[0]!.body).toMatchObject({
      expected_version: 2,
      settings: {
        azure_devops: {
          category_work_item_types: { incident: 'Bug', bug: 'Bug', feature_request: 'User Story' },
        },
        priority: {
          category_default_priority: { incident: 'high', bug: 'critical', other: 'low' },
          azure_devops_priority: { critical: 1, high: 1, medium: 2, low: 4 },
        },
      },
    });
  });

  it('clears a category work item type', async () => {
    const user = userEvent.setup();
    const { calls } = setup();
    const select = await screen.findByRole('combobox', { name: 'Work item type for Bug' });
    expect(select).toHaveValue('Bug');
    // Mantine's clear control sits next to the input.
    const clear = select.parentElement!.querySelector('button')!;
    await user.click(clear);
    expect(select).toHaveValue('');
    await user.click(screen.getByRole('button', { name: 'Save settings' }));
    await screen.findByText('Settings saved');
    const body = callsTo(calls, 'PUT', SETTINGS)[0]!.body as { settings: TenantSettings };
    expect(body.settings.azure_devops.category_work_item_types).toEqual({ incident: 'Bug' });
  });

  it('blocks saving on client validation errors and shows them on the fields', async () => {
    const user = userEvent.setup();
    const { calls } = setup();
    const medium = await screen.findByRole('textbox', { name: 'Medium ≥' });
    await user.clear(medium);
    await user.type(medium, '0.9');
    // Out-of-range retention is clamped to the allowed minimum when the input loses focus.
    const audit = screen.getByRole('textbox', { name: 'Audit events (days)' });
    await user.clear(audit);
    await user.type(audit, '7');
    await user.click(screen.getByRole('button', { name: 'Save settings' }));

    expect(await screen.findByText('Medium threshold must be ≤ high threshold')).toBeInTheDocument();
    expect(medium).toHaveAttribute('aria-invalid', 'true');
    expect(audit).toHaveValue('30');
    expect(callsTo(calls, 'PUT', SETTINGS)).toHaveLength(0);
  });

  it('does not silently save an emptied confidence threshold as 0 (regression)', async () => {
    const user = userEvent.setup();
    const { calls } = setup();
    const medium = await screen.findByRole('textbox', { name: 'Medium ≥' });
    await user.clear(medium);
    await user.click(screen.getByRole('button', { name: 'Save settings' }));
    expect(await screen.findByText('Must be between 0 and 1')).toBeInTheDocument();
    expect(medium).toHaveAttribute('aria-invalid', 'true');
    expect(callsTo(calls, 'PUT', SETTINGS)).toHaveLength(0);
  });

  it('maps 422 field errors onto inputs and lists every server message', async () => {
    const user = userEvent.setup();
    const { calls } = setup({
      put: () =>
        errorResponse(422, 'validation_failed', {
          message: 'The request failed validation.',
          details: [
            { loc: ['body', 'settings', 'confidence', 'medium'], msg: 'medium must be <= high' },
            {
              field: 'settings.retention.audit_days',
              message: 'Input should be greater than or equal to 30',
            },
            { msg: 'Value error, at least one category must be enabled' },
          ],
        }),
    });
    await user.type(await screen.findByRole('textbox', { name: 'Default project' }), 'X');
    await user.click(screen.getByRole('button', { name: 'Save settings' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The server rejected these settings');
    const items = within(alert)
      .getAllByRole('listitem')
      .map((li) => li.textContent);
    expect(items).toEqual([
      'confidence.medium: medium must be <= high',
      'retention.audit_days: Input should be greater than or equal to 30',
      'Value error, at least one category must be enabled',
    ]);
    expect(screen.getByRole('textbox', { name: 'Medium ≥' })).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText('medium must be <= high')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Audit events (days)' })).toHaveAttribute(
      'aria-invalid',
      'true',
    );
    expect(callsTo(calls, 'PUT', SETTINGS)).toHaveLength(1);

    // A later successful save clears the server errors.
    vi.unstubAllGlobals();
    setupPutOk(calls);
    await user.click(screen.getByRole('button', { name: 'Save settings' }));
    await waitFor(() => expect(screen.queryByText('The server rejected these settings')).toBeNull());
  });

  it('shows the 422 message when the server gives no field details', async () => {
    const user = userEvent.setup();
    setup({
      put: () => errorResponse(422, 'validation_failed', { message: 'Settings are invalid.', details: [] }),
    });
    await user.type(await screen.findByRole('textbox', { name: 'Default project' }), 'X');
    await user.click(screen.getByRole('button', { name: 'Save settings' }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('listitem')).toHaveTextContent('Settings are invalid.');
  });

  it('shows a reload banner on 409 stale_version, locks the form and reloads on demand', async () => {
    const user = userEvent.setup();
    let version = 2;
    const { calls } = setup({
      get: () => settingsOut({ version }),
      put: () => {
        version = 5;
        return errorResponse(409, 'stale_version', { message: 'Settings were modified.' });
      },
    });
    await user.type(await screen.findByRole('textbox', { name: 'Default project' }), 'X');
    await user.click(screen.getByRole('button', { name: 'Save settings' }));

    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent('Settings changed — reload before saving');
    expect(screen.getByRole('textbox', { name: 'Default project' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save settings' })).toBeNull();

    await user.click(within(banner).getByRole('button', { name: 'Reload settings' }));
    await waitFor(() => expect(screen.queryByText('Settings changed — reload before saving')).toBeNull());
    expect(screen.getByText('Version 5')).toBeInTheDocument();
    const project = screen.getByRole('textbox', { name: 'Default project' });
    expect(project).toBeEnabled();
    expect(project).toHaveValue('Payments');
    expect(callsTo(calls, 'GET', SETTINGS)).toHaveLength(2);
  });

  it('reports other save failures in a toast with the support reference', async () => {
    const user = userEvent.setup();
    setup({
      put: () =>
        errorResponse(500, 'internal_error', { message: 'Database unavailable', correlationId: 'corr-put' }),
    });
    await user.type(await screen.findByRole('textbox', { name: 'Default project' }), 'X');
    await user.click(screen.getByRole('button', { name: 'Save settings' }));
    expect(await screen.findByText('Could not save settings')).toBeInTheDocument();
    expect(screen.getByText('internal_error · ref corr-put')).toBeInTheDocument();
    // Edits are kept so the admin can retry.
    expect(screen.getByRole('textbox', { name: 'Default project' })).toHaveValue('PaymentsX');
  });
});

/** Swap the fetch mock for one whose PUT succeeds (keeps the same call log). */
function setupPutOk(calls: ReturnType<typeof mockFetch>['calls']) {
  const next = mockFetch([
    { method: 'GET', path: SETTINGS, handler: () => jsonResponse(settingsOut()) },
    {
      method: 'PUT',
      path: SETTINGS,
      handler: (call) => {
        const body = call.body as { expected_version: number; settings: TenantSettings };
        return jsonResponse(settingsOut({ version: body.expected_version + 1, settings: body.settings }));
      },
    },
  ]);
  next.calls.push(...calls);
}
