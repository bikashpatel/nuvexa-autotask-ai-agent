import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  AdoProjectsOut,
  AdoWorkItemTypesOut,
  ConnectionOut,
  HealthOut,
  IntegrationsOut,
  MeOut,
} from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { PERSONAS, authConfig, integrationsOut } from '../../test/fixtures';
import {
  callsTo,
  errorResponse,
  jsonResponse,
  mockFetch,
  renderWithProviders,
  type Route,
} from '../../test/utils';
import { IntegrationsPage } from './IntegrationsPage';

const INTEGRATIONS = '/api/v1/integrations';
const HEALTH = '/api/v1/health/ready';
const PROJECTS = '/api/v1/integrations/azure-devops/projects';

type Body<T> = () => T | Response;
const respond = <T,>(body: Body<T>) => {
  const value = body();
  return value instanceof Response ? value : jsonResponse(value);
};

function connection(overrides: Partial<ConnectionOut> = {}): ConnectionOut {
  return {
    id: 'conn-graph',
    provider: 'microsoft_graph',
    status: 'active',
    email: 'dana@contoso.example',
    display_name: 'Dana (Graph)',
    granted_scopes: ['Mail.Read', 'offline_access'],
    connected_at: '2026-10-24T08:00:00Z',
    last_error: null,
    sync: {
      folder: 'Inbox',
      last_status: 'failed',
      last_sync_started_at: '2026-10-24T09:00:00Z',
      last_sync_completed_at: null,
      last_error: 'Graph throttled the request',
      messages_ingested_total: 1284,
    },
    ...overrides,
  };
}

function setup({
  integrations = () => integrationsOut(),
  health = () => ({ status: 'ok', version: '0.4.2', checks: { database: 'ok', worker: 'ok' } }),
  projects = () => ({ items: [] }),
  types = () => ({ project: 'Payments', items: [] }),
  session = PERSONAS.approver,
  extra = [],
}: {
  integrations?: Body<IntegrationsOut>;
  health?: Body<HealthOut>;
  projects?: Body<AdoProjectsOut>;
  types?: (project: string) => AdoWorkItemTypesOut | Response;
  session?: MeOut;
  extra?: Route[];
} = {}) {
  const { calls } = mockFetch([
    { method: 'GET', path: INTEGRATIONS, handler: () => respond(integrations) },
    { method: 'GET', path: HEALTH, handler: () => respond(health) },
    { method: 'GET', path: '/api/v1/auth/config', handler: () => jsonResponse(authConfig()) },
    { method: 'GET', path: PROJECTS, handler: () => respond(projects) },
    {
      method: 'GET',
      path: /^\/api\/v1\/integrations\/azure-devops\/projects\/[^/]+\/work-item-types$/,
      handler: (call) => {
        const project = decodeURIComponent(call.url.split('/')[6]!);
        return respond(() => types(project));
      },
    },
    ...extra,
  ]);
  const result = renderWithProviders(<IntegrationsPage />, { session, withNotifications: true });
  return { ...result, calls };
}

function cardOf(text: string): HTMLElement {
  return screen.getByText(text).closest('.mantine-Card-root') as HTMLElement;
}

function clearCookies() {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

beforeEach(() => clearCookies());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  clearCookies();
});

describe('<IntegrationsPage> states', () => {
  it('shows a skeleton while loading', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => undefined)),
    );
    renderWithProviders(<IntegrationsPage />);
    expect(screen.getByRole('heading', { level: 1, name: 'Integrations' })).toBeInTheDocument();
    expect(screen.getByLabelText('Loading integrations')).toHaveAttribute('aria-busy', 'true');
  });

  it('shows the error with its support reference and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = setup({
      integrations: () =>
        fail ? errorResponse(403, 'permission_denied', { correlationId: 'corr-int' }) : integrationsOut(),
    });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('You do not have access to this');
    expect(alert).toHaveTextContent('corr-int');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('region', { name: 'Microsoft 365 mailbox' })).toBeInTheDocument();
    expect(callsTo(calls, 'GET', INTEGRATIONS)).toHaveLength(2);
  });
});

describe('<IntegrationsPage> Microsoft 365', () => {
  it('renders the demo setup: demo alert, mock badges, scopes and the demo mailbox; no accessibility violations', async () => {
    setup();
    const ms = within(await screen.findByRole('region', { name: 'Microsoft 365 mailbox' }));
    expect(screen.getByText(/A synthetic mailbox, the heuristic analysis agent/)).toBeInTheDocument();
    expect(ms.getByText('Demo')).toBeInTheDocument();
    expect(ms.getAllByText('Mock').length).toBeGreaterThanOrEqual(2);
    expect(ms.getByText('offline_access')).toBeInTheDocument();
    expect(ms.getByText('Connections (1)')).toBeInTheDocument();

    const demo = within(cardOf('Demo mailbox'));
    expect(demo.getByText('demo@contoso.example · Demo mailbox')).toBeInTheDocument();
    expect(demo.getByLabelText('Status: Active')).toBeInTheDocument();
    expect(demo.getByLabelText('Status: Synced')).toBeInTheDocument();
    expect(demo.getByText('Inbox · 42 ingested')).toBeInTheDocument();
    expect(demo.getByRole('button', { name: 'Sync now' })).toBeEnabled();
    // The demo mailbox cannot be disconnected.
    expect(demo.queryByRole('button', { name: 'Disconnect' })).toBeNull();

    // Entra not configured → the connect button is disabled and explains why.
    const connect = ms.getByRole('button', { name: 'Connect a mailbox' });
    expect(connect).toBeDisabled();
    expect(connect).toHaveAttribute('aria-describedby', 'ms-connect-reason');
    expect(document.getElementById('ms-connect-reason')).toHaveTextContent(
      'Microsoft Entra ID is not configured on the API.',
    );

    await expectNoSeriousA11yViolations();
  });

  it('links to the Microsoft connect flow when Entra is configured', async () => {
    const base = integrationsOut();
    setup({
      integrations: () =>
        integrationsOut({
          demo_mode: false,
          microsoft: { ...base.microsoft, mode: 'graph', entra_configured: true, connections: [] },
        }),
    });
    const ms = within(await screen.findByRole('region', { name: 'Microsoft 365 mailbox' }));
    expect(ms.getByRole('link', { name: 'Connect a mailbox' })).toHaveAttribute(
      'href',
      `/api/v1/integrations/microsoft/connect?redirect_to=${encodeURIComponent('/integrations')}`,
    );
    expect(ms.getByText('Graph')).toBeInTheDocument();
    expect(ms.queryByText('Mock')).toBeNull();
    expect(ms.getByText('No mailbox connected yet.')).toBeInTheDocument();
    expect(screen.queryByText(/A synthetic mailbox, the heuristic analysis agent/)).toBeNull();
  });

  it('shows connection errors, sync failures and disables sync for inactive connections', async () => {
    const base = integrationsOut();
    setup({
      integrations: () =>
        integrationsOut({
          microsoft: {
            ...base.microsoft,
            connections: [
              connection({ status: 'needs_reauth', last_error: 'Refresh token expired' }),
              connection({
                id: 'conn-new',
                display_name: null,
                email: null,
                granted_scopes: [],
                connected_at: null,
                sync: null,
              }),
            ],
          },
        }),
    });
    const graph = within(
      await screen.findByText('Dana (Graph)').then((el) => el.closest('.mantine-Card-root') as HTMLElement),
    );
    expect(graph.getByText('Refresh token expired')).toBeInTheDocument();
    expect(graph.getByLabelText('Status: Needs re-authentication')).toBeInTheDocument();
    expect(graph.getByLabelText('Status: Sync failed')).toBeInTheDocument();
    expect(graph.getByText('Graph throttled the request')).toBeInTheDocument();
    expect(graph.getByText('Inbox · 1,284 ingested')).toBeInTheDocument();
    expect(graph.getByRole('button', { name: 'Sync now' })).toBeDisabled();
    expect(graph.getByRole('button', { name: 'Disconnect' })).toBeEnabled();

    const fresh = within(cardOf('Mailbox'));
    expect(fresh.getByText('— · Microsoft graph')).toBeInTheDocument();
    expect(fresh.getByText('Never synced')).toBeInTheDocument();
    expect(fresh.queryByText('Last sync completed')).toBeNull();
    expect(fresh.getByText('Granted scopes').nextSibling).toHaveTextContent('—');
  });

  it('starts a sync for one connection', async () => {
    const user = userEvent.setup();
    const { calls } = setup({
      extra: [
        {
          method: 'POST',
          path: '/api/v1/emails/sync',
          handler: () => jsonResponse({ job_ids: ['job-1'], message: 'Sync queued.' }, 202),
        },
      ],
    });
    await user.click(
      await within(
        await screen.findByText('Demo mailbox').then((el) => el.closest('.mantine-Card-root') as HTMLElement),
      ).findByRole('button', { name: 'Sync now' }),
    );
    expect(await screen.findByText('Mailbox sync queued (1 job)')).toBeInTheDocument();
    expect(callsTo(calls, 'POST', '/api/v1/emails/sync')[0]!.body).toEqual({
      connected_account_id: 'conn-1',
    });
  });

  it('disconnects a mailbox after confirmation, with CSRF, then refetches', async () => {
    const user = userEvent.setup();
    document.cookie = 'nuvexa_csrf=csrf-int; path=/';
    const base = integrationsOut();
    let connections = [connection()];
    const { calls } = setup({
      integrations: () => integrationsOut({ microsoft: { ...base.microsoft, connections } }),
      extra: [
        {
          method: 'DELETE',
          path: '/api/v1/integrations/connections/conn-graph',
          handler: () => {
            connections = [];
            return new Response(null, { status: 204 });
          },
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: 'Disconnect' }));
    const dialog = await screen.findByRole('dialog', { name: 'Disconnect this mailbox?' });
    expect(dialog).toHaveTextContent('deletes the stored (encrypted) tokens');
    await user.click(within(dialog).getByRole('button', { name: 'Disconnect' }));

    expect(await screen.findByText('Disconnected')).toBeInTheDocument();
    const del = callsTo(calls, 'DELETE', '/api/v1/integrations/connections/conn-graph');
    expect(del).toHaveLength(1);
    expect(del[0]!.headers.get('X-CSRF-Token')).toBe('csrf-int');
    expect(await screen.findByText('No mailbox connected yet.')).toBeInTheDocument();
    expect(callsTo(calls, 'GET', INTEGRATIONS)).toHaveLength(2);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('can back out of disconnecting, and reports failures with the support reference', async () => {
    const user = userEvent.setup();
    const base = integrationsOut();
    const { calls } = setup({
      integrations: () => integrationsOut({ microsoft: { ...base.microsoft, connections: [connection()] } }),
      extra: [
        {
          method: 'DELETE',
          path: '/api/v1/integrations/connections/conn-graph',
          handler: () =>
            errorResponse(500, 'internal_error', { message: 'Vault unavailable', correlationId: 'corr-del' }),
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: 'Disconnect' }));
    let dialog = await screen.findByRole('dialog', { name: 'Disconnect this mailbox?' });
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(callsTo(calls, 'DELETE', '/api/v1/integrations/connections/conn-graph')).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: 'Disconnect' }));
    dialog = await screen.findByRole('dialog', { name: 'Disconnect this mailbox?' });
    await user.click(within(dialog).getByRole('button', { name: 'Disconnect' }));
    expect(await screen.findByText('Could not disconnect')).toBeInTheDocument();
    expect(screen.getByText('Vault unavailable')).toBeInTheDocument();
    expect(screen.getByText('internal_error · ref corr-del')).toBeInTheDocument();
    // Still listed: nothing changes until the server confirms.
    expect(screen.getByText('Dana (Graph)')).toBeInTheDocument();
  });

  it('shows the viewer no mutating controls', async () => {
    const base = integrationsOut();
    setup({
      session: PERSONAS.viewer,
      integrations: () =>
        integrationsOut({
          microsoft: {
            ...base.microsoft,
            entra_configured: true,
            connections: [connection(), ...base.microsoft.connections],
          },
        }),
    });
    expect(await screen.findByText('Dana (Graph)')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sync now' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Connect a mailbox' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Connect a mailbox' })).toBeNull();
  });
});

describe('<IntegrationsPage> Azure DevOps', () => {
  it('renders the mock configuration and facts', async () => {
    setup();
    const ado = within(await screen.findByRole('region', { name: 'Azure DevOps' }));
    expect(ado.getByText(/Mock mode: work items are simulated/)).toBeInTheDocument();
    // Mode badge ("Mock") plus the explicit Mock marker.
    expect(ado.getAllByText('Mock')).toHaveLength(2);
    expect(ado.getByText('Organization').nextSibling).toHaveTextContent('contoso');
    expect(ado.getByText('Configured').nextSibling).toHaveTextContent('Yes');
    expect(ado.getByText('Default project').nextSibling).toHaveTextContent('Payments');
    expect(ado.getByText('Default work item type').nextSibling).toHaveTextContent('Task');
    expect(ado.getByText('API version').nextSibling).toHaveTextContent('7.1');
    expect(ado.getByText('Per-user connection').nextSibling).toHaveTextContent('Not required');
    expect(ado.queryByRole('link', { name: /Azure DevOps/ })).toBeNull();
  });

  it.each([
    [false, 'Not connected', 'Connect Azure DevOps'],
    [true, 'Connected', 'Reconnect Azure DevOps'],
  ])('per-user connection (connected=%s) shows %s and a %s link', async (connected, label, linkName) => {
    const base = integrationsOut();
    setup({
      integrations: () =>
        integrationsOut({
          azure_devops: {
            ...base.azure_devops,
            mode: 'live',
            organization: null,
            default_project: null,
            configured: false,
            requires_user_connection: true,
            user_connected: connected,
          },
        }),
    });
    const ado = within(await screen.findByRole('region', { name: 'Azure DevOps' }));
    expect(ado.getByText('Per-user connection').nextSibling).toHaveTextContent(label);
    expect(ado.getByRole('link', { name: linkName })).toHaveAttribute(
      'href',
      `/api/v1/integrations/azure-devops/connect?redirect_to=${encodeURIComponent('/integrations')}`,
    );
    expect(ado.getByText('Organization').nextSibling).toHaveTextContent('—');
    expect(ado.getByText('Configured').nextSibling).toHaveTextContent('No');
    expect(ado.queryByText(/Mock mode/)).toBeNull();
  });

  it('tests the connection and shows the result', async () => {
    const user = userEvent.setup();
    let ok = true;
    setup({
      extra: [
        {
          method: 'GET',
          path: '/api/v1/integrations/azure-devops/test',
          handler: () =>
            jsonResponse({
              ok,
              mode: 'mock',
              organization: ok ? 'contoso' : null,
              message: ok ? 'Mock organization reachable.' : 'PAT rejected.',
              latency_ms: ok ? 12 : null,
            }),
        },
      ],
    });
    const ado = within(await screen.findByRole('region', { name: 'Azure DevOps' }));
    await user.click(ado.getByRole('button', { name: 'Test connection' }));
    const result = await ado.findByRole('status');
    expect(result).toHaveTextContent('Connection OK');
    expect(result).toHaveTextContent('Mock organization reachable.');
    expect(result).toHaveTextContent('Mode mock · organization contoso · 12 ms');

    ok = false;
    await user.click(ado.getByRole('button', { name: 'Test connection' }));
    expect(await ado.findByText('Connection failed')).toBeInTheDocument();
    expect(ado.getByRole('status')).toHaveTextContent('Mode mock · organization — · —');
  });

  it('reports a failed connection test request in a toast with the support reference', async () => {
    const user = userEvent.setup();
    setup({
      extra: [
        {
          method: 'GET',
          path: '/api/v1/integrations/azure-devops/test',
          handler: () =>
            errorResponse(502, 'upstream_error', {
              message: 'Azure DevOps unreachable',
              correlationId: 'corr-ado',
            }),
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: 'Test connection' }));
    expect(await screen.findByText('Connection test failed')).toBeInTheDocument();
    expect(screen.getByText('upstream_error · ref corr-ado')).toBeInTheDocument();
  });

  it('lists projects, loads work item types for the chosen project (URL-encoded) and links safely', async () => {
    const user = userEvent.setup();
    const { calls } = setup({
      projects: () => ({
        items: [
          { id: 'p1', name: 'Payments', state: 'wellFormed', url: 'https://dev.azure.com/contoso/Payments' },
          { id: 'p2', name: 'Contoso Web', state: null, url: 'mock://contoso/Contoso Web' },
          { id: 'p3', name: 'Ops', state: null, url: null },
        ],
      }),
      types: (project) => ({
        project,
        items:
          project === 'Payments'
            ? [
                {
                  name: 'Bug',
                  reference_name: 'Microsoft.VSTS.WorkItemTypes.Bug',
                  description: 'A defect',
                  is_disabled: false,
                },
                { name: 'Legacy', reference_name: null, description: null, is_disabled: true },
              ]
            : [
                {
                  name: 'Task',
                  reference_name: 'Microsoft.VSTS.WorkItemTypes.Task',
                  description: null,
                  is_disabled: false,
                },
              ],
      }),
    });
    const ado = within(await screen.findByRole('region', { name: 'Azure DevOps' }));
    expect(callsTo(calls, 'GET', PROJECTS)).toHaveLength(0);
    await user.click(ado.getByRole('button', { name: 'List projects' }));

    // The default project is preselected and its types are loaded.
    const types = await ado.findByRole('table', { name: 'Work item types in Payments' });
    const rows = within(types).getAllByRole('row').slice(1);
    expect(rows[0]).toHaveTextContent('BugMicrosoft.VSTS.WorkItemTypes.BugEnabled');
    expect(rows[1]).toHaveTextContent('Legacy—Disabled');
    expect(ado.getByRole('button', { name: 'List projects' })).toBeDisabled();
    const projectLink = ado.getByRole('link', { name: /Open project/ });
    expect(projectLink).toHaveAttribute('href', 'https://dev.azure.com/contoso/Payments');
    expect(projectLink).toHaveAttribute('rel', 'noopener noreferrer');
    expect(projectLink).toHaveAttribute('target', '_blank');

    await user.click(ado.getByRole('combobox', { name: 'Project' }));
    expect(await screen.findByRole('option', { name: 'Payments (wellFormed)' })).toBeInTheDocument();
    await user.click(screen.getByRole('option', { name: 'Contoso Web' }));
    expect(await ado.findByRole('table', { name: 'Work item types in Contoso Web' })).toHaveTextContent(
      'Task',
    );
    expect(
      calls.some((c) => c.url === '/api/v1/integrations/azure-devops/projects/Contoso%20Web/work-item-types'),
    ).toBe(true);
    // mock:// project URLs are text with a Mock badge, never a link.
    expect(ado.getByText('mock://contoso/Contoso Web').closest('a')).toBeNull();
    expect(ado.queryByRole('link', { name: /Open project/ })).toBeNull();

    await user.click(ado.getByRole('combobox', { name: 'Project' }));
    await user.click(await screen.findByRole('option', { name: 'Ops' }));
    expect(await ado.findByRole('table', { name: 'Work item types in Ops' })).toBeInTheDocument();
    expect(ado.queryByText(/Open project/)).toBeNull();
    expect(ado.queryByText('mock://contoso/Contoso Web')).toBeNull();
  });

  it('shows an empty project list without loading types', async () => {
    const user = userEvent.setup();
    const base = integrationsOut();
    const { calls } = setup({
      integrations: () => integrationsOut({ azure_devops: { ...base.azure_devops, default_project: null } }),
    });
    await user.click(await screen.findByRole('button', { name: 'List projects' }));
    const select = await screen.findByRole('combobox', { name: 'Project' });
    expect(select).toHaveAttribute('placeholder', 'No projects visible');
    expect(calls.some((c) => c.url.includes('/work-item-types'))).toBe(false);
  });

  it('shows project and work item type errors with retry', async () => {
    const user = userEvent.setup();
    let projectsFail = true;
    let typesFail = true;
    const { calls } = setup({
      projects: () =>
        projectsFail
          ? errorResponse(502, 'upstream_error', { correlationId: 'corr-projects' })
          : { items: [{ id: 'p1', name: 'Payments', state: null, url: null }] },
      types: (project) =>
        typesFail ? errorResponse(404, 'not_found', { correlationId: 'corr-types' }) : { project, items: [] },
    });
    const ado = within(await screen.findByRole('region', { name: 'Azure DevOps' }));
    await user.click(ado.getByRole('button', { name: 'List projects' }));
    const alertWith = async (text: string) =>
      (await ado.findByText(text)).closest('[role="alert"]') as HTMLElement;
    const projectsAlert = await alertWith('corr-projects');
    expect(projectsAlert).toHaveTextContent('upstream_error, HTTP 502');
    projectsFail = false;
    await user.click(within(projectsAlert).getByRole('button', { name: 'Retry' }));

    const typesAlert = await alertWith('corr-types');
    expect(typesAlert).toHaveTextContent('Not found');
    expect(await ado.findByRole('combobox', { name: 'Project' })).toHaveValue('Payments');
    typesFail = false;
    await user.click(within(typesAlert).getByRole('button', { name: 'Retry' }));
    expect(await ado.findByRole('table', { name: 'Work item types in Payments' })).toBeInTheDocument();
    expect(callsTo(calls, 'GET', PROJECTS)).toHaveLength(2);
    expect(callsTo(calls, 'GET', `${PROJECTS}/Payments/work-item-types`)).toHaveLength(2);
  });
});

describe('<IntegrationsPage> LLM and diagnostics', () => {
  it('renders the LLM provider facts', async () => {
    const base = integrationsOut();
    setup({
      integrations: () =>
        integrationsOut({
          llm: {
            ...base.llm,
            provider: 'openai',
            model: 'gpt-4o',
            structured_outputs: false,
            observability_enabled: true,
          },
        }),
    });
    const llm = within(await screen.findByRole('region', { name: 'LLM provider' }));
    expect(llm.getByText('Provider').nextSibling).toHaveTextContent('openai');
    expect(llm.getByText('Model').nextSibling).toHaveTextContent('gpt-4o');
    expect(llm.getByText('Structured outputs').nextSibling).toHaveTextContent('No');
    expect(llm.getByText('Observability').nextSibling).toHaveTextContent('Enabled');
    expect(llm.getByText('Openai')).toBeInTheDocument();
    expect(llm.queryByText('Mock')).toBeNull();
  });

  it('shows API readiness, environment, role and permissions, and re-checks on demand', async () => {
    const user = userEvent.setup();
    let status: HealthOut['status'] = 'ok';
    const { calls } = setup({
      health: () => ({ status, version: '0.4.2', checks: { database: 'ok', job_queue: 'lagging' } }),
    });
    const diag = within(await screen.findByRole('region', { name: 'Diagnostics' }));
    expect(await diag.findByText('Ok')).toBeInTheDocument();
    expect(diag.getByText('v0.4.2')).toBeInTheDocument();
    expect(diag.getByText('Job queue:')).toBeInTheDocument();
    expect(diag.getByText('lagging')).toBeInTheDocument();
    expect(await diag.findByText('development')).toBeInTheDocument();
    expect(diag.getByText('Tenant').nextSibling).toHaveTextContent('Contoso');
    expect(diag.getByText('Your role').nextSibling).toHaveTextContent('Approver');
    for (const permission of PERSONAS.approver.permissions)
      expect(diag.getByText(permission)).toBeInTheDocument();
    expect(diag.getByText('Dashboard build').nextSibling).toHaveTextContent('test');
    expect(diag.getByText('Demo mode').nextSibling).toHaveTextContent('Yes');

    status = 'degraded';
    await user.click(diag.getByRole('button', { name: 'Re-check' }));
    expect(await diag.findByText('Degraded')).toBeInTheDocument();
    status = 'error';
    await user.click(diag.getByRole('button', { name: 'Re-check' }));
    expect(await diag.findByText('Error')).toBeInTheDocument();
    expect(callsTo(calls, 'GET', HEALTH)).toHaveLength(3);
  });

  it('shows a failed readiness check as an error with the support reference', async () => {
    setup({ health: () => errorResponse(503, 'not_ready', { correlationId: 'corr-ready' }) });
    const diag = within(await screen.findByRole('region', { name: 'Diagnostics' }));
    const alert = await diag.findByRole('alert');
    expect(alert).toHaveTextContent('API readiness check failed');
    expect(alert).toHaveTextContent('corr-ready');
  });
});
