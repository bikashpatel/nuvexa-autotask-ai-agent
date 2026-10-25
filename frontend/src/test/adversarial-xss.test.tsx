/**
 * Adversarial QA — untrusted content in the dashboard (CLAUDE.md invariant 4: email content and model
 * output are untrusted and never rendered as HTML).
 *
 * Every page that displays email content, AI output, workflow/audit data, integration data or API
 * error text is rendered with a hostile value in every free-text field. The whole document is then
 * checked by `injectionViolations()`:
 *  - no element was created from payload markup (script, img, iframe, base, meta, form, math, …),
 *  - no element carries an inline event-handler attribute (on*), or a srcdoc,
 *  - payload text only reaches text nodes or text-bearing attributes (aria-label, title, value, …),
 *  - every href/src/action/formaction uses http(s)/mailto or is relative; links that leave the app
 *    open in a new tab, and every target=_blank link carries rel="noopener noreferrer",
 *  - the planted `window.__nuvexaXss` probe was never called.
 * Each test also asserts that the hostile text is visible verbatim (shown as text, not dropped).
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { navigationUrl, safeRedirectPath } from '../api/client';
import type {
  ActionDetail,
  ActionOut,
  AdoConnectionTestOut,
  AdoProjectsOut,
  AdoValidateOut,
  AdoWorkItemTypesOut,
  AnalysisOut,
  AuditEventOut,
  DashboardSummaryOut,
  EmailDetail,
  EmailListItem,
  FollowUpTaskOut,
  HealthOut,
  IntegrationsOut,
  ReplyDraftOut,
  WorkflowDetail,
} from '../api/types';
import { ExternalLink } from '../components/ExternalLink';
import { ActionCard, CurrentApproval } from '../features/actions/ActionCard';
import { ActionControls } from '../features/actions/ActionControls';
import { ActionReviewPage } from '../features/actions/ActionReviewPage';
import { AnalysisPanel } from '../features/actions/AnalysisPanel';
import { ApprovalsPage } from '../features/approvals/ApprovalsPage';
import { AuditLogPage } from '../features/audit/AuditLogPage';
import { LoginPage } from '../features/auth/LoginPage';
import { EmailDetailPage } from '../features/inbox/EmailDetailPage';
import { EmailHeaders } from '../features/inbox/EmailParts';
import { InboxPage } from '../features/inbox/InboxPage';
import { IntegrationsPage } from '../features/integrations/IntegrationsPage';
import { ActivityFeed, OverviewPage } from '../features/overview/OverviewPage';
import { AppLayout } from '../features/shell/AppLayout';
import { TasksPage } from '../features/tasks/TasksPage';
import { WorkflowDetailPage } from '../features/workflows/WorkflowDetailPage';
import { WorkflowsPage } from '../features/workflows/WorkflowsPage';
import { classifyUrl } from '../lib/urls';
import {
  ALL_ACTION_PERMISSIONS,
  adoAction,
  analysis,
  approvalFixture,
  auditEvent,
  authConfig,
  dashboardSummary,
  emailDetail,
  emailListItem,
  executionFixture,
  followUpAction,
  integrationsOut,
  noActionAction,
  page,
  replyAction,
  replyDraftFixture,
  sessionFor,
  taskFixture,
  workflowDetail,
  workflowEvent,
  workflowRun,
} from './fixtures';
import { errorResponse, jsonResponse, mockFetch, renderWithProviders, type Route } from './utils';

// --- Hostile values ------------------------------------------------------------------------------

const PROBE = 'xss-probe';
const SPY = '__nuvexaXss';

/** One value packing every HTML/URL/template/mXSS vector; `field` makes each occurrence traceable. */
function hostile(field: string): string {
  const call = `window.${SPY}('${field}')`;
  return [
    `[${PROBE}:${field}]`,
    `<img src=x onerror="${call}">`,
    `<svg onload="${call}"><use href="javascript:${call}"/></svg>`,
    `<script>${call}</script>`,
    `<iframe srcdoc="<script>${call}</script>"></iframe>`,
    `<a href="javascript:${call}" target="_blank">open</a>`,
    `<math><mtext><table><mglyph><style><img src=x onerror="${call}">`,
    `<base href="https://evil.example/"><meta http-equiv="refresh" content="0;url=https://evil.example">`,
    `<form action="https://evil.example/steal"><button formaction="javascript:${call}">go</button></form>`,
    `[click me](javascript:${call}) **bold** <b>b</b> &lt;i&gt; &#60;img&#62;`,
    `{{constructor.constructor('${call}')()}} \${${call}}`,
    `javascript:${call} https://evil.example/phish www.evil.example mailto:ceo@evil.example`,
    `\u202Etxt.exe\u202C zero\u200B\u200C\u200D\u2060\uFEFFwidth`,
  ].join(' ');
}
const H = hostile;

/** URLs that must never become a link (any scheme other than http(s), or tricks around it). */
const UNSAFE_URLS: string[] = [
  `javascript:window.${SPY}('url')`,
  `JaVaScRiPt:window.${SPY}('url')`,
  ` \tjavascript:window.${SPY}('url')`,
  `java\tscript:window.${SPY}('url')`,
  `java\nscript:window.${SPY}('url')`,
  `\u0000javascript:window.${SPY}('url')`,
  `\u00a0javascript:window.${SPY}('url')`,
  `\uFEFFjavascript:window.${SPY}('url')`,
  `javascript&colon;window.${SPY}('url')`,
  'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
  `data:text/html,<script>window.${SPY}('url')</script>`,
  'vbscript:msgbox("x")',
  'file:///etc/passwd',
  'blob:https://evil.example/0b9f',
  '//evil.example/phish',
  '\\\\evil.example\\phish',
  '/\\evil.example/phish',
  'https:evil.example/phish',
  'ms-officecmd:{"id":3}',
  'intent://evil.example#Intent;scheme=https;end',
  'about:blank',
];

// --- The DOM invariant checker --------------------------------------------------------------------

const FORBIDDEN_TAGS = new Set([
  'script',
  'iframe',
  'frame',
  'frameset',
  'object',
  'embed',
  'applet',
  'portal',
  'base',
  'meta',
  'link',
  'img',
  'image',
  'picture',
  'source',
  'video',
  'audio',
  'track',
  'math',
  'mglyph',
  'template',
  'form',
  'use',
]);
const URL_ATTRIBUTES = new Set([
  'href',
  'xlink:href',
  'src',
  'action',
  'formaction',
  'poster',
  'data',
  'background',
  'ping',
  'srcset',
  'codebase',
  'longdesc',
  'manifest',
  'icon',
]);
/** Attributes React legitimately fills with untrusted *text* (escaped, never parsed as markup). */
const TEXT_ATTRIBUTES = new Set([
  'aria-label',
  'aria-description',
  'aria-valuetext',
  'title',
  'value',
  'placeholder',
  'alt',
]);
const SAFE_PROTOCOLS = new Set(['http:', 'https:', 'mailto:']);

function resolve(raw: string): URL | null {
  try {
    return new URL(raw.trim(), `${window.location.origin}/`);
  } catch {
    return null;
  }
}

function describeEl(el: Element): string {
  return `<${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}>`;
}

/** Every way the document could be executing or linking attacker-controlled content. */
function injectionViolations(root: ParentNode = document): string[] {
  const out: string[] = [];
  for (const el of Array.from(root.querySelectorAll('*'))) {
    const tag = el.tagName.toLowerCase();
    if (FORBIDDEN_TAGS.has(tag)) out.push(`forbidden element ${describeEl(el)}`);
    if (tag === 'style' && el.textContent?.includes(PROBE)) out.push('payload inside a <style> element');
    for (const name of el.getAttributeNames()) {
      const lower = name.toLowerCase();
      const value = el.getAttribute(name) ?? '';
      if (lower.startsWith('on')) out.push(`${describeEl(el)} has inline handler ${name}`);
      if (lower === 'srcdoc') out.push(`${describeEl(el)} has srcdoc`);
      if (lower === 'style' && /javascript:|expression\(|-moz-binding|behavior:/i.test(value)) {
        out.push(`${describeEl(el)} has a scriptable style "${value.slice(0, 60)}"`);
      }
      if (URL_ATTRIBUTES.has(lower)) {
        const url = resolve(value);
        if (!url) out.push(`${describeEl(el)} ${name} is not a URL: "${value.slice(0, 60)}"`);
        else if (!SAFE_PROTOCOLS.has(url.protocol)) {
          out.push(`${describeEl(el)} ${name} uses ${url.protocol} ("${value.slice(0, 60)}")`);
        }
      }
      if (value.includes(PROBE) && !TEXT_ATTRIBUTES.has(lower)) {
        out.push(`payload text reached attribute ${name} of ${describeEl(el)}`);
      }
    }
    if (tag === 'a' && el.hasAttribute('href')) {
      const url = resolve(el.getAttribute('href') ?? '');
      const blank = el.getAttribute('target') === '_blank';
      const rel = (el.getAttribute('rel') ?? '').toLowerCase().split(/\s+/);
      if (blank && !(rel.includes('noopener') && rel.includes('noreferrer'))) {
        out.push(`target=_blank link without rel="noopener noreferrer": ${el.getAttribute('href')}`);
      }
      if (url && url.origin !== window.location.origin && !blank) {
        out.push(`cross-origin link that does not open in a new tab: ${el.getAttribute('href')}`);
      }
    }
  }
  return out;
}

/** Links pointing at the attacker host planted in the hostile text (auto-linking would create them). */
function linksToAttacker(): string[] {
  return Array.from(document.querySelectorAll('[href]'))
    .map((el) => el.getAttribute('href') ?? '')
    .filter((href) => resolve(href)?.hostname.endsWith('evil.example'));
}

let xssSpy: ReturnType<typeof vi.fn>;

function expectNoInjection() {
  expect(injectionViolations()).toEqual([]);
  expect(linksToAttacker()).toEqual([]);
  expect(xssSpy).not.toHaveBeenCalled();
}

/** Match text exactly as rendered: the default normalizer turns U+FEFF (a JS `\s`) into a space. */
const RAW = { normalizer: (text: string) => text };

/** How a string looks inside pretty-printed JSON (JsonBlock): quotes and backslashes escaped. */
const asJson = (value: string) => JSON.stringify(value).slice(1, -1);

/** The hostile value is on screen verbatim, as text. */
function expectVisibleText(...values: string[]) {
  const text = document.body.textContent ?? '';
  for (const value of values) expect(text, `missing text for ${value.slice(0, 40)}`).toContain(value);
}

beforeEach(() => {
  xssSpy = vi.fn();
  vi.stubGlobal(SPY, xssSpy);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// --- Hostile API world ------------------------------------------------------------------------------

const UNSAFE_URL = `javascript:window.${SPY}('link')`;
const SAFE_ADO_URL = 'https://dev.azure.com/contoso/Payments/_workitems/edit/4711';

function hostileEmailItem(): EmailListItem {
  return emailListItem({
    subject: H('subject'),
    sender_name: H('sender_name'),
    sender_address: H('sender_address'),
    body_preview: H('body_preview'),
    has_attachments: true,
    content_changed_since_analysis: true,
  });
}

function hostileEmail(): EmailDetail {
  return emailDetail({
    ...hostileEmailItem(),
    internet_message_id: H('internet_message_id'),
    to_recipients: [{ name: H('to_name'), address: H('to_address') }],
    cc_recipients: [{ name: null, address: H('cc_address') }],
    body_text: `${H('body_text')}\n<a href="https://evil.example/login">Reset your password</a>\n${H('body_line2')}`,
    attachments: [
      { name: H('attachment_name'), content_type: H('attachment_type'), size: 2048, is_inline: false },
    ],
    web_link: UNSAFE_URL,
    importance: H('importance'),
  });
}

function hostileAnalysis(): AnalysisOut {
  return analysis({
    classification: {
      rationale: H('classification_rationale'),
      missing_information: [H('missing_information')],
      extraction_warnings: [H('extraction_warning')],
    },
    extraction: {
      summary: H('extraction_summary'),
      requester_name: H('requester_name'),
      requester_email: H('requester_email'),
      organization: H('organization'),
      product_or_system: H('product_or_system'),
      environment: H('environment'),
      impact: H('impact'),
      suggested_owner: H('suggested_owner'),
      suggested_team: H('suggested_team'),
      suggested_work_item_type: H('suggested_work_item_type'),
      reference_urls: [UNSAFE_URL, 'https://evil.example/reference', H('reference_url')],
      reference_ids: [H('reference_id')],
      evidence: [{ field: H('evidence_field'), quote: H('evidence_quote') }],
    },
    injection_flags: [
      { rule: 'ignore_previous_instructions', severity: 'high', excerpt: H('injection_excerpt') },
    ],
    validation_warnings: [H('analysis_warning')],
    model_provider: H('model_provider'),
    model_name: H('model_name'),
    prompt_version: H('prompt_version'),
    schema_version: H('schema_version'),
    confidence_method: H('confidence_method'),
  });
}

function hostileAdo(): ActionDetail {
  return adoAction({
    title: H('action_title'),
    rationale: H('action_rationale'),
    expected_impact: H('expected_impact'),
    validation_warnings: [H('action_warning')],
    last_error: H('last_error'),
    external_id: H('external_id'),
    external_url: UNSAFE_URL,
    email_subject: H('email_subject'),
    permissions: ALL_ACTION_PERMISSIONS,
    payload: {
      kind: 'create_azure_devops_work_item',
      project: H('payload_project'),
      work_item_type: H('payload_type'),
      title: H('payload_title'),
      description: H('payload_description'),
      priority: 1,
      area_path: H('area_path'),
      iteration_path: H('iteration_path'),
      tags: [H('tag')],
      assigned_to: H('assigned_to'),
      additional_fields: { 'Microsoft.VSTS.Common.Severity': H('additional_field') },
      source_email: { email_id: 'email-1', subject: H('source_subject') },
    },
    current_approval: approvalFixture({
      decided_by_name: H('decided_by'),
      reason: H('approval_reason'),
      invalidated_at: '2026-10-24T10:06:00Z',
      invalidated_reason: H('invalidated_reason'),
    }),
    approvals: [
      approvalFixture({
        decided_by_name: H('history_decided_by'),
        reason: H('history_reason'),
        invalidated_at: '2026-10-24T10:06:00Z',
        invalidated_reason: H('history_invalidated_reason'),
      }),
    ],
    executions: [
      executionFixture({
        status: 'failed',
        external_id: H('execution_external_id'),
        external_url: `data:text/html,<script>window.${SPY}('execution')</script>`,
        error_category: 'transient',
        error_message: H('execution_error'),
      }),
      executionFixture({ id: 'ex-2', attempt_number: 2, external_url: SAFE_ADO_URL }),
    ],
  });
}

function hostileReply(): ActionDetail {
  return replyAction({
    title: H('reply_title'),
    rationale: H('reply_rationale'),
    validation_errors: [H('reply_validation_error')],
    payload: {
      kind: 'propose_email_reply',
      to: [H('reply_to')],
      subject: H('reply_subject'),
      body: H('reply_body'),
      in_reply_to_internet_message_id: H('reply_in_reply_to'),
      source_email: { email_id: 'email-1' },
    },
  });
}

function hostileFollowUp(): ActionDetail {
  return followUpAction({
    title: H('follow_title'),
    payload: {
      kind: 'create_follow_up_task',
      title: H('follow_payload_title'),
      description: H('follow_payload_description'),
      due_at: '2026-11-02T09:30:00Z',
      source_email: { email_id: 'email-1' },
    },
  });
}

function hostileNoAction(): ActionDetail {
  return noActionAction({
    title: H('none_title'),
    payload: { kind: 'mark_no_action', reason: H('none_reason'), source_email: { email_id: 'email-1' } },
  });
}

function summaryOf(detail: ActionDetail): ActionOut {
  const { approvals: _a, executions: _e, ...rest } = detail;
  return rest;
}

function hostileAudit(): AuditEventOut[] {
  return [
    auditEvent({
      actor_name: H('actor_name'),
      event_type: H('event_type'),
      resource_type: H('resource_type'),
      resource_id: H('resource_id'),
      action_id: null,
      workflow_run_id: null,
      correlation_id: H('audit_correlation'),
      details: { reason: H('audit_details'), nested: { html: H('audit_nested') } },
    }),
    auditEvent({ id: 'au-2', actor_name: H('actor_name_2'), details: {} }),
  ];
}

interface World {
  emailItem: EmailListItem;
  email: EmailDetail;
  workflow: WorkflowDetail;
  actions: Record<string, ActionDetail>;
  audit: AuditEventOut[];
  tasks: FollowUpTaskOut[];
  drafts: ReplyDraftOut[];
  integrations: IntegrationsOut;
  summary: DashboardSummaryOut;
  projects: AdoProjectsOut;
  types: AdoWorkItemTypesOut;
  adoTest: AdoConnectionTestOut;
  validate: AdoValidateOut;
  health: HealthOut;
}

function hostileWorld(): World {
  const actions = [hostileAdo(), hostileReply(), hostileFollowUp(), hostileNoAction()];
  return {
    emailItem: hostileEmailItem(),
    email: hostileEmail(),
    workflow: workflowDetail({
      run: workflowRun({
        error_category: 'transient',
        error_message: H('run_error'),
        current_step: H('current_step'),
        model_provider: H('run_model_provider'),
        model_name: H('run_model_name'),
        prompt_version: H('run_prompt_version'),
        schema_version: H('run_schema_version'),
        correlation_id: H('run_correlation'),
        email_subject: H('run_email_subject'),
        metrics: {
          total_tokens: 1234,
          [H('metric_key')]: H('metric_value'),
          nested: { html: H('metric_nested') },
        },
      }),
      analysis: hostileAnalysis(),
      actions: actions.map((a) =>
        summaryOf({
          ...a,
          external_url: a.id === 'act-1' ? UNSAFE_URL : null,
          external_id: H('table_external_id'),
        }),
      ),
      events: [
        workflowEvent({
          step: H('event_step'),
          message: H('event_message'),
          details: {
            model: H('event_model'),
            provider: H('event_provider'),
            tool_name: H('event_tool'),
            arguments: { title: H('event_argument'), url: UNSAFE_URL },
            error: H('event_error'),
            error_category: H('event_error_category'),
            usage: { input_tokens: 800, output_tokens: 120 },
          },
        }),
        workflowEvent({
          id: 'ev-2',
          event_type: 'step_failed',
          status: 'failed',
          message: H('event_message_2'),
        }),
      ],
    }),
    actions: Object.fromEntries(actions.map((a) => [a.id, a])),
    audit: hostileAudit(),
    tasks: [taskFixture({ title: H('task_title'), description: H('task_description') })],
    drafts: [
      replyDraftFixture({
        to_recipients: [H('draft_to')],
        subject: H('draft_subject'),
        body: H('draft_body'),
      }),
    ],
    integrations: integrationsOut({
      microsoft: {
        ...integrationsOut().microsoft,
        connections: [
          {
            id: 'conn-1',
            provider: 'microsoft_graph',
            status: 'error',
            email: H('connection_email'),
            display_name: H('connection_name'),
            granted_scopes: [H('granted_scope')],
            connected_at: '2026-10-24T08:00:00Z',
            last_error: H('connection_error'),
            sync: {
              folder: H('sync_folder'),
              last_status: 'failed',
              last_sync_started_at: '2026-10-24T09:00:00Z',
              last_sync_completed_at: null,
              last_error: H('sync_error'),
              messages_ingested_total: 3,
            },
          },
        ],
      },
      azure_devops: {
        ...integrationsOut().azure_devops,
        organization: H('ado_organization'),
        default_project: H('project_name'),
        default_work_item_type: H('ado_default_type'),
        api_version: H('ado_api_version'),
      },
      llm: {
        provider: H('llm_provider'),
        model: H('llm_model'),
        structured_outputs: true,
        observability_enabled: false,
      },
    }),
    summary: dashboardSummary({
      recent_activity: hostileAudit(),
      categories: { [H('category_key')]: 3, incident: 2 },
    }),
    projects: { items: [{ id: 'p-1', name: H('project_name'), state: H('project_state'), url: UNSAFE_URL }] },
    types: {
      project: H('project_name'),
      items: [
        {
          name: H('type_name'),
          reference_name: H('type_reference'),
          description: H('type_description'),
          is_disabled: false,
        },
      ],
    },
    adoTest: {
      ok: false,
      mode: H('ado_test_mode'),
      organization: H('ado_test_org'),
      message: H('ado_test_message'),
      latency_ms: 12,
    },
    validate: {
      valid: false,
      schema_issues: [{ field: H('schema_field'), message: H('schema_issue') }],
      policy_issues: [H('policy_issue')],
      server_issues: [{ field: null, message: H('server_issue') }],
      server_validated: true,
    },
    health: {
      status: 'degraded',
      version: H('api_version'),
      checks: { database: H('check_value'), [H('check_name')]: 'ok' },
    },
  };
}

function worldRoutes(world: World): Route[] {
  return [
    { method: 'GET', path: '/api/v1/emails', handler: () => jsonResponse(page([world.emailItem])) },
    { method: 'GET', path: /^\/api\/v1\/emails\/[^/]+$/, handler: () => jsonResponse(world.email) },
    { method: 'GET', path: '/api/v1/workflows', handler: () => jsonResponse(page([world.workflow.run])) },
    {
      method: 'GET',
      path: /^\/api\/v1\/workflows\/[^/]+\/analysis$/,
      handler: () => jsonResponse(world.workflow.analysis),
    },
    { method: 'GET', path: /^\/api\/v1\/workflows\/[^/]+$/, handler: () => jsonResponse(world.workflow) },
    {
      method: 'GET',
      path: '/api/v1/actions',
      handler: () => jsonResponse(page(Object.values(world.actions).map(summaryOf))),
    },
    { method: 'GET', path: /^\/api\/v1\/actions\/[^/]+\/history$/, handler: () => jsonResponse(world.audit) },
    {
      method: 'GET',
      path: /^\/api\/v1\/actions\/[^/]+$/,
      handler: (call) => {
        const id = decodeURIComponent(call.url.split('?')[0]!.split('/').pop() ?? '');
        const detail = world.actions[id];
        return detail ? jsonResponse(detail) : errorResponse(404, 'not_found');
      },
    },
    { method: 'GET', path: '/api/v1/integrations', handler: () => jsonResponse(world.integrations) },
    {
      method: 'GET',
      path: '/api/v1/integrations/azure-devops/projects',
      handler: () => jsonResponse(world.projects),
    },
    {
      method: 'GET',
      path: /^\/api\/v1\/integrations\/azure-devops\/projects\/[^/]+\/work-item-types$/,
      handler: () => jsonResponse(world.types),
    },
    {
      method: 'GET',
      path: '/api/v1/integrations/azure-devops/test',
      handler: () => jsonResponse(world.adoTest),
    },
    {
      method: 'POST',
      path: '/api/v1/integrations/azure-devops/validate',
      handler: () => jsonResponse(world.validate),
    },
    { method: 'GET', path: '/api/v1/health/ready', handler: () => jsonResponse(world.health) },
    {
      method: 'GET',
      path: '/api/v1/auth/config',
      handler: () => jsonResponse(authConfig({ environment: H('environment_name') })),
    },
    { method: 'GET', path: '/api/v1/audit-events', handler: () => jsonResponse(page(world.audit)) },
    { method: 'GET', path: '/api/v1/tasks', handler: () => jsonResponse(page(world.tasks)) },
    { method: 'GET', path: '/api/v1/reply-drafts', handler: () => jsonResponse(page(world.drafts)) },
    { method: 'GET', path: '/api/v1/dashboard/summary', handler: () => jsonResponse(world.summary) },
  ];
}

function setup(ui: ReactNode, opts: { path?: string; route?: string } = {}, world = hostileWorld()) {
  const { calls } = mockFetch(worldRoutes(world));
  const view = renderWithProviders(ui, { withNotifications: true, ...opts });
  return { ...view, calls, world, user: userEvent.setup() };
}

async function openAll(user: ReturnType<typeof userEvent.setup>, buttons: HTMLElement[]) {
  for (const button of buttons) {
    if (button.getAttribute('aria-expanded') !== 'true') await user.click(button);
  }
}

// --- 0. Static scan + checker canary ------------------------------------------------------------------

const PRODUCTION_SOURCES = import.meta.glob<string>(
  ['../**/*.{ts,tsx}', '!../**/*.test.{ts,tsx}', '!./**', '!../**/*.d.ts'],
  { query: '?raw', import: 'default', eager: true },
);

describe('static scan: HTML-injection and code-evaluation sinks in production code', () => {
  it('scans the real source tree', () => {
    const files = Object.keys(PRODUCTION_SOURCES);
    expect(files.length).toBeGreaterThan(40);
    expect(files.some((f) => f.endsWith('components/PlainText.tsx'))).toBe(true);
    expect(files.some((f) => f.includes('.test.') || f.includes('/test/'))).toBe(false);
  });

  it.each([
    ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
    ['innerHTML / outerHTML', /\.(inner|outer)HTML\b/],
    ['insertAdjacentHTML', /insertAdjacentHTML/],
    ['document.write', /document\.write(ln)?\s*\(/],
    ['createContextualFragment', /createContextualFragment/],
    ['DOMParser', /\bDOMParser\b/],
    ['iframe srcdoc', /\bsrc[dD]oc\b/],
    ['eval()', /(^|[^\w.])eval\s*\(/],
    ['new Function()', /\bnew\s+Function\s*\(/],
    ['string timers', /set(Timeout|Interval)\s*\(\s*['"`]/],
  ])('no %s', (_label, pattern) => {
    const offenders = Object.entries(PRODUCTION_SOURCES)
      .filter(([, source]) => pattern.test(source))
      .map(([file]) => file);
    expect(offenders).toEqual([]);
  });
});

describe('the invariant checker (canary: it must flag real injection)', () => {
  it('flags markup, handlers, unsafe hrefs and missing rel', () => {
    const host = document.createElement('div');
    document.body.append(host);
    host.insertAdjacentHTML(
      'beforeend',
      `<img src="x" onerror="void 0"><svg onload="void 0"></svg>` +
        `<a href="javascript:void 0">a</a><a href="https://example.com" target="_blank">b</a>` +
        `<a href="https://example.com">c</a><span class="${PROBE}">d</span>`,
    );
    const violations = injectionViolations();
    host.remove();
    expect(violations).toEqual(
      expect.arrayContaining([
        'forbidden element <img>',
        '<img> has inline handler onerror',
        '<svg> has inline handler onload',
        '<a> href uses javascript: ("javascript:void 0")',
        'target=_blank link without rel="noopener noreferrer": https://example.com',
        'cross-origin link that does not open in a new tab: https://example.com',
        'payload text reached attribute class of <span>',
      ]),
    );
  });
});

// --- 1. XSS by surface ------------------------------------------------------------------------------

describe('XSS payloads are inert on every surface that shows untrusted content', () => {
  it('Inbox list: subject, sender, address and preview', async () => {
    setup(<InboxPage />);
    expect(await screen.findByRole('table', { name: 'Emails' })).toBeInTheDocument();
    expectVisibleText(H('subject'), H('sender_name'), H('sender_address'), H('body_preview'));
    expectNoInjection();
  });

  it('Email detail: headers, recipients, body with HTML/links, attachments, web_link, latest analysis', async () => {
    setup(<EmailDetailPage />, { path: '/emails/:emailId', route: '/emails/email-1' });
    expect(await screen.findByRole('heading', { level: 1, name: H('subject') })).toBeInTheDocument();
    await screen.findByText('Latest analysis');
    expectVisibleText(
      H('body_text'),
      '<a href="https://evil.example/login">Reset your password</a>',
      H('to_name'),
      H('cc_address'),
      H('internet_message_id'),
      H('attachment_name'),
      H('attachment_type'),
      H('run_error'),
      UNSAFE_URL, // web_link: shown as text, never a link
    );
    // Proposed action titles of the latest run (links to the review page) are text too.
    expect(screen.getAllByRole('link').every((a) => (a.getAttribute('href') ?? '').startsWith('/'))).toBe(
      true,
    );
    expectNoInjection();
  });

  it('Action review: email, analysis, injection warning, all four action cards, accordions and approve modal', async () => {
    const { user } = setup(<ActionReviewPage />, { path: '/actions/:actionId', route: '/actions/act-1' });
    const card = await screen.findByTestId('action-card-act-1');
    await screen.findByTestId('action-card-act-reply');
    await screen.findByTestId('action-card-act-follow');
    await screen.findByTestId('action-card-act-none');
    await screen.findByTestId('injection-warning');
    await screen.findByText(H('body_text'), { ...RAW, exact: false });

    await openAll(
      user,
      screen.getAllByRole('button', {
        name: /^(Execution results|Approval history|Audit trail|Raw payload)/,
      }),
    );
    await waitFor(() =>
      expect(within(card).getAllByText(H('actor_name'), { ...RAW, exact: false }).length).toBeGreaterThan(0),
    );
    // Validate with Azure DevOps (server-side issue messages are untrusted too).
    await user.click(within(card).getByRole('button', { name: 'Validate with Azure DevOps' }));
    await within(card).findByText('Payload has issues');

    expectVisibleText(
      H('action_title'),
      H('action_rationale'),
      H('expected_impact'),
      H('action_warning'),
      H('last_error'),
      H('external_id'),
      H('approval_reason'),
      H('history_reason'),
      H('execution_error'),
      H('execution_external_id'),
      H('classification_rationale'),
      H('missing_information'),
      H('extraction_warning'),
      H('extraction_summary'),
      H('organization'),
      H('impact'),
      H('evidence_quote'),
      H('injection_excerpt'),
      H('reference_url'),
      H('reply_validation_error'),
      H('reply_title'),
      H('follow_title'),
      H('none_title'),
      H('policy_issue'),
      H('server_issue'),
      UNSAFE_URL, // action.external_url and the reference URL: text, not links
    );
    // Editable payload fields hold the hostile text as input values (never parsed).
    expect(within(card).getByRole('textbox', { name: /^Title/ })).toHaveValue(H('payload_title'));
    expect(within(card).getByRole('textbox', { name: /^Description/ })).toHaveValue(H('payload_description'));
    // The one safe http(s) work-item URL is a proper external link.
    const external = screen.getAllByRole('link', { name: /Open work item/ });
    expect(external.map((a) => a.getAttribute('href'))).toEqual([SAFE_ADO_URL]);
    expect(external[0]).toHaveAttribute('target', '_blank');
    expect(external[0]).toHaveAttribute('rel', 'noopener noreferrer');
    expectNoInjection();

    // Approve modal quotes the (hostile) title.
    await user.click(within(card).getByTestId('approve-button'));
    const dialog = await screen.findByRole('dialog', { name: 'Approve this exact payload?' });
    expect(dialog.textContent).toContain(H('action_title'));
    expectNoInjection();
  });

  it('Approvals queue: action title and email subject', async () => {
    setup(<ApprovalsPage />);
    expect(await screen.findByText(H('action_title'), RAW)).toBeInTheDocument();
    expectVisibleText(H('email_subject'));
    expectNoInjection();
  });

  it('Workflows list: email subject', async () => {
    setup(<WorkflowsPage />);
    expect(await screen.findByText(H('run_email_subject'), RAW)).toBeInTheDocument();
    expectNoInjection();
  });

  it('Workflow detail: run facts, metrics, event timeline (expanded), actions table and audit trail', async () => {
    const { user } = setup(<WorkflowDetailPage />, {
      path: '/workflows/:workflowId',
      route: '/workflows/run-1',
    });
    expect(
      await screen.findByRole('heading', { level: 1, name: H('run_email_subject') }),
    ).toBeInTheDocument();
    await openAll(user, screen.getAllByRole('button', { name: /Show details/ }));
    await screen.findAllByText(H('actor_name'), RAW);
    expectVisibleText(
      H('run_error'),
      H('run_correlation'),
      H('run_prompt_version'),
      H('metric_value'),
      H('event_message'),
      H('event_message_2'),
      H('event_tool'),
      H('event_error'),
      asJson(H('event_argument')),
      H('action_title'),
    );
    expectNoInjection();
  });

  it('Audit log: actor, event type, resource, correlation id and expanded JSON details', async () => {
    const { user } = setup(<AuditLogPage />);
    expect(await screen.findByRole('table', { name: 'Audit events' })).toBeInTheDocument();
    await openAll(user, screen.getAllByRole('button', { name: /^Show details for/ }));
    expectVisibleText(H('actor_name'), H('event_type'), H('resource_id'), H('audit_correlation'));
    // Details JSON is text: quotes are JSON-escaped, markup stays literal.
    expectVisibleText(asJson(H('audit_details')), asJson(H('audit_nested')));
    expectNoInjection();
  });

  it('Overview: recent activity and category distribution with hostile keys', async () => {
    setup(<OverviewPage />);
    expect(await screen.findByText('Emails analyzed')).toBeInTheDocument();
    expectVisibleText(H('actor_name'));
    expectNoInjection();
  });

  it('Tasks: title and description', async () => {
    setup(<TasksPage />);
    expect(await screen.findByText(H('task_title'), RAW)).toBeInTheDocument();
    expectVisibleText(H('task_description'));
    expectNoInjection();
  });

  it('Reply drafts tab: recipients, subject, body', async () => {
    setup(<TasksPage />, { route: '/tasks?tab=drafts' });
    expect(await screen.findByText(H('draft_subject'), RAW)).toBeInTheDocument();
    expectVisibleText(H('draft_body'), H('draft_to'));
    expectNoInjection();
  });

  it('Integrations: mailbox connection, sync errors, ADO test message, projects (javascript: url), types, LLM, diagnostics', async () => {
    const { user } = setup(<IntegrationsPage />);
    expect(await screen.findByText(H('connection_name'), RAW)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Test connection' }));
    expect(await screen.findByText(H('ado_test_message'), RAW)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'List projects' }));
    expect(await screen.findByText(H('type_name'), RAW)).toBeInTheDocument();
    expectVisibleText(
      H('connection_error'),
      H('sync_error'),
      H('ado_organization'),
      H('type_reference'),
      UNSAFE_URL,
    );
    expect(screen.queryByRole('link', { name: /Open project/ })).toBeNull();
    expectNoInjection();
  });

  it('App shell: identity claims (display name, email, tenant) in the header and account menu', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/auth/config',
        handler: () => jsonResponse(authConfig({ environment: H('env') })),
      },
      { method: 'GET', path: '/api/v1/actions', handler: () => jsonResponse(page([], { total: 3 })) },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<AppLayout />, {
      session: sessionFor('approver', {
        display_name: H('display_name'),
        email: H('user_email'),
        tenant_name: H('tenant_name'),
      }),
    });
    await user.click(await screen.findByRole('button', { name: `Account menu for ${H('display_name')}` }));
    expect(await screen.findByText(H('user_email'), RAW)).toBeInTheDocument();
    expectVisibleText(H('display_name'), H('tenant_name'));
    expectNoInjection();
  });

  it('API error text in an error toast: message, code and correlation id', async () => {
    const hostileError = () =>
      errorResponse(500, H('error_code'), {
        message: H('error_message'),
        correlationId: H('error_correlation'),
      });
    mockFetch([
      { method: 'GET', path: '/api/v1/emails', handler: () => jsonResponse(page([hostileEmailItem()])) },
      { method: 'POST', path: '/api/v1/emails/sync', handler: hostileError },
      { method: 'GET', path: /^\/api\/v1\/emails\/[^/]+$/, handler: hostileError },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<InboxPage />, { withNotifications: true });
    await user.click(await screen.findByRole('button', { name: 'Sync mailbox' }));
    await waitFor(() => expect(document.body.textContent).toContain(H('error_message')));
    expectNoInjection();
  });

  it('ErrorState panel on a failed detail load', async () => {
    mockFetch([
      {
        method: 'GET',
        path: /^\/api\/v1\/emails\/[^/]+$/,
        handler: () =>
          errorResponse(500, H('panel_code'), {
            message: H('panel_message'),
            correlationId: H('panel_correlation'),
          }),
      },
    ]);
    renderWithProviders(<EmailDetailPage />, { path: '/emails/:emailId', route: '/emails/email-1' });
    expect((await screen.findByRole('alert')).textContent).toContain(H('panel_message'));
    expectVisibleText(H('panel_correlation'));
    expectNoInjection();
  });
});

// --- 2. URLs that become href/src -------------------------------------------------------------------

describe('URL fields that become links', () => {
  it.each(UNSAFE_URLS.map((url) => [JSON.stringify(url), url]))('%s is never a link', (_label, url) => {
    expect(classifyUrl(url)).not.toBe('http');
    renderWithProviders(<ExternalLink url={url}>Open work item</ExternalLink>);
    expect(screen.queryByRole('link')).toBeNull();
    expect(document.body.textContent).toContain(url);
    expectNoInjection();
  });

  it.each([
    'https://dev.azure.com/contoso/Payments/_workitems/edit/4711',
    'HTTPS://DEV.AZURE.COM/contoso/_workitems/edit/1',
    'http://ado.internal.example/tfs/DefaultCollection/_workitems/edit/9',
    `https://dev.azure.com/contoso/_workitems/edit/1?q="><img src=x onerror="window.${SPY}('q')">`,
  ])('%s opens in a new tab with rel="noopener noreferrer"', (url) => {
    renderWithProviders(<ExternalLink url={url}>Open work item</ExternalLink>);
    const link = screen.getByRole('link', { name: /Open work item/ });
    expect(link).toHaveAttribute('href', url);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(injectionViolations()).toEqual([]);
  });

  it('mock:// URLs are text with a Mock badge', () => {
    renderWithProviders(<ExternalLink url="mock://contoso/Payments/_workitems/edit/4711" />);
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByText('Mock')).toBeInTheDocument();
  });
});

describe('post-login redirect target (?redirect_to=) cannot leave the app', () => {
  // The URL parser strips ASCII tab/newline anywhere in a URL, so "/\t/evil.example" becomes the
  // protocol-relative "//evil.example". React Router's history.push falls back to
  // window.location.assign(url) when pushState rejects a cross-origin URL, so any caller that
  // navigates (without `replace`) to such a value is an open redirect.
  const VECTORS = [
    'https://evil.example/phish',
    '//evil.example',
    '/\\evil.example',
    '\\\\evil.example',
    'javascript:alert(1)',
    'data:text/html,x',
    ' //evil.example',
    '/\t/evil.example',
    '/\n/evil.example',
    '/\r\n/evil.example',
    '/\t\\evil.example',
  ];

  // Regression (Prompt 5): safeRedirectPath is an allow-list, so "/\t/evil.example" (and \n, \r variants) fall back to "/".
  it.each(VECTORS.map((v) => [JSON.stringify(v), v]))(
    'safeRedirectPath(%s) stays on this origin',
    (_label, value) => {
      const target = safeRedirectPath(value);
      expect(new URL(target, window.location.href).origin).toBe(window.location.origin);
    },
  );

  it('every in-app path survives unchanged', () => {
    for (const path of ['/', '/inbox?q=a%20b', '/actions/act-1', '/approvals?view=attention#x']) {
      expect(safeRedirectPath(path)).toBe(path);
    }
  });

  it('the Microsoft sign-in link is same-origin and carries only a safe redirect_to', async () => {
    mockFetch([
      { method: 'GET', path: '/api/v1/auth/me', handler: () => errorResponse(401, 'not_authenticated') },
      {
        method: 'GET',
        path: '/api/v1/auth/config',
        handler: () => jsonResponse(authConfig({ entra_login_enabled: true })),
      },
    ]);
    renderWithProviders(<LoginPage />, {
      session: null,
      route: `/login?redirect_to=${encodeURIComponent(`javascript:window.${SPY}(1)`)}&error=${encodeURIComponent(H('signin_error'))}`,
    });
    const link = await screen.findByRole('link', { name: 'Sign in with Microsoft' });
    expect(link).toHaveAttribute('href', navigationUrl('/auth/login', '/'));
    expect(new URL(link.getAttribute('href') ?? '', window.location.href).origin).toBe(
      window.location.origin,
    );
    // The ?error= value is never echoed (a fixed message is shown instead).
    expect(document.body.textContent).not.toContain(PROBE);
    expectNoInjection();
  });
});

// --- 3. Long strings, bidi overrides and invisible characters -------------------------------------

const LONG = `${'A'.repeat(4000)}${'%'.repeat(1000)}`;

/** CSS that makes an unbroken token wrap or clip inside its box (inline style, app class or Mantine prop). */
function hasWrapGuard(start: Element | null, depth = 3): boolean {
  for (let el = start, i = 0; el && i <= depth; el = el.parentElement, i += 1) {
    const style = el.getAttribute('style') ?? '';
    if (/overflow-wrap:\s*(anywhere|break-word)|word-break:\s*(break-all|break-word)/.test(style))
      return true;
    if (el.classList.contains('nuvexa-plain-text')) return true; // global.css: overflow-wrap:anywhere
    if (el.hasAttribute('data-truncate') || el.hasAttribute('data-line-clamp')) return true;
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return true;
  }
  return false;
}

function elementsShowing(value: string): Element[] {
  return Array.from(document.body.querySelectorAll('*')).filter((el) =>
    Array.from(el.childNodes).some(
      (n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? '').includes(value),
    ),
  );
}

describe('extremely long unbroken strings', () => {
  it('the primary review surfaces wrap (or clip) a 5,000-character token instead of overflowing', async () => {
    const world = hostileWorld();
    world.email = { ...world.email, subject: LONG, body_text: LONG };
    world.workflow = {
      ...world.workflow,
      analysis: analysis({
        classification: { rationale: LONG },
        extraction: { evidence: [{ field: 'impact', quote: LONG }] },
      }),
    };
    world.actions['act-1'] = {
      ...world.actions['act-1']!,
      title: LONG,
      rationale: LONG,
      email_subject: LONG,
    };
    setup(<ActionReviewPage />, { path: '/actions/:actionId', route: '/actions/act-1' }, world);
    await screen.findByTestId('action-card-act-1');
    await screen.findByText('AI analysis');
    await waitFor(() => expect(elementsShowing(LONG).length).toBeGreaterThanOrEqual(6));
    const unguarded = elementsShowing(LONG)
      .filter((el) => !hasWrapGuard(el))
      .map((el) => `${describeEl(el)} ${el.className}`.trim());
    expect(unguarded).toEqual([]);
  });

  it('long values survive intact in the editable payload (no silent truncation before approval)', async () => {
    const world = hostileWorld();
    const ado = world.actions['act-1']!;
    world.actions['act-1'] = {
      ...ado,
      payload: {
        ...ado.payload,
        title: LONG.slice(0, 255),
        description: LONG,
      },
    };
    setup(<ActionReviewPage />, { path: '/actions/:actionId', route: '/actions/act-1' }, world);
    const card = await screen.findByTestId('action-card-act-1');
    expect(within(card).getByRole('textbox', { name: /^Title/ })).toHaveValue(LONG.slice(0, 255));
    expect(within(card).getByRole('textbox', { name: /^Description/ })).toHaveValue(LONG);
  });
});

describe('bidi overrides and zero-width characters', () => {
  const SPOOF = `Pay invoice\u202Efdp.exe\u202C to\u200B dana@fabrikam\u200D.example\u2066x\u2069`;

  it('are kept verbatim: the reviewer edits/approves exactly the code points that will be written', async () => {
    const world = hostileWorld();
    const ado = world.actions['act-1']!;
    world.actions['act-1'] = {
      ...ado,
      title: SPOOF,
      payload: {
        ...ado.payload,
        title: SPOOF,
      },
    };
    setup(<ActionReviewPage />, { path: '/actions/:actionId', route: '/actions/act-1' }, world);
    const card = await screen.findByTestId('action-card-act-1');
    expect(within(card).getByRole('textbox', { name: /^Title/ })).toHaveValue(SPOOF);
    expect(within(card).getByRole('heading', { level: 3 }).textContent).toBe(SPOOF);
    expectNoInjection();
  });
});

// Regression (Prompt 5): untrusted values quoted inside trusted sentences are bidi-isolated (<bdi>), so a
// U+202E (RIGHT-TO-LEFT OVERRIDE) without a closing U+202C cannot reorder the trusted text around it.
describe('bidi isolation of untrusted values inside trusted sentences', () => {
  // An unterminated override: without isolation, everything after it in the paragraph renders reversed
  // (e.g. "v3 · approved" in the reviewer's sentence).
  const RLO = 'Dana‮ 4v ,devorppa';

  /** `sentence` shows `value` only inside a <bdi> holding exactly that value; nothing leaks outside. */
  function expectIsolated(sentence: Element | null, value: string) {
    expect(sentence).not.toBeNull();
    const isolates = Array.from(sentence!.querySelectorAll('bdi')).map((el) => el.textContent);
    expect(isolates).toContain(value);
    const outside = sentence!.cloneNode(true) as Element;
    for (const el of Array.from(outside.querySelectorAll('bdi'))) el.remove();
    expect(outside.textContent).not.toContain('‮');
    // …and the trusted part of the sentence is still there, outside the isolate.
    expect(outside.textContent?.trim().length).toBeGreaterThan(0);
  }

  /** The innermost element whose full text matches. */
  function sentenceMatching(pattern: RegExp): Element {
    const matches = screen.getAllByText((_, el) => pattern.test(el?.textContent ?? ''));
    return matches.at(-1)!;
  }

  const detailRoutes = (detail: ActionDetail, history: AuditEventOut[] = []): Route[] => [
    { method: 'GET', path: '/api/v1/actions/act-1', handler: () => jsonResponse(detail) },
    { method: 'GET', path: '/api/v1/actions/act-1/history', handler: () => jsonResponse(history) },
    {
      method: 'GET',
      path: '/api/v1/integrations/azure-devops/projects',
      handler: () => jsonResponse({ items: [] }),
    },
  ];

  it('Approve dialog: the proposal title inside "You are approving … “title”. The approval is bound …"', async () => {
    const user = userEvent.setup();
    mockFetch([]);
    renderWithProviders(
      <ActionControls
        action={adoAction({ title: RLO, permissions: ALL_ACTION_PERMISSIONS })}
        dirty={false}
        editable={false}
        saving={false}
        queued={false}
        onSave={() => undefined}
        onDiscard={() => undefined}
        onConflict={() => undefined}
        onQueued={() => undefined}
      />,
    );
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    const dialog = await screen.findByRole('dialog', { name: 'Approve this exact payload?' });
    const sentence = within(dialog).getByText(
      (_, el) => el?.tagName === 'P' && /You are approving/.test(el.textContent ?? ''),
    );
    expectIsolated(sentence, RLO);
    expect(sentence.textContent).toMatch(/The approval is bound to this version and payload hash/);
  });

  it('Action card: reviewer and actor names in the approval banner, approval history and audit trail', async () => {
    const user = userEvent.setup();
    const current = approvalFixture({ decided_by_name: RLO });
    mockFetch(
      detailRoutes(
        adoAction({
          status: 'approved',
          current_approval: current,
          approvals: [current],
          permissions: ALL_ACTION_PERMISSIONS,
        }),
        [auditEvent({ actor_name: RLO })],
      ),
    );
    renderWithProviders(<ActionCard actionId="act-1" />);
    await screen.findByTestId('action-card-act-1');
    expectIsolated(sentenceMatching(/^Approved by /), RLO);

    await user.click(screen.getByRole('button', { name: 'Approval history (1)' }));
    expectIsolated(sentenceMatching(/ · v3 · /), RLO);

    await user.click(screen.getByRole('button', { name: 'Audit trail' }));
    const actor = await screen.findByText(
      (_, el) =>
        el?.tagName === 'P' &&
        (el.textContent ?? '').startsWith(`${RLO} · `) &&
        !(el.textContent ?? '').includes(' · v3 · '),
    );
    expectIsolated(actor, RLO);
  });

  it('Current approval that is no longer valid: "Approval by <name> is no longer valid"', () => {
    const stale = approvalFixture({ decided_by_name: RLO, action_version: 2 });
    renderWithProviders(<CurrentApproval action={adoAction({ version: 3, current_approval: stale })} />);
    expectIsolated(sentenceMatching(/^Approval by .* is no longer valid$/), RLO);
  });

  it.each([
    [RLO, RLO],
    [null, 'a reviewer'],
  ])('Save notice: "The previous approval by <%j> (v3) was invalidated …"', async (name, shown) => {
    const user = userEvent.setup();
    const current = approvalFixture({ decided_by_name: name });
    let detail = adoAction({
      status: 'approved',
      current_approval: current,
      approvals: [current],
      permissions: ALL_ACTION_PERMISSIONS,
    });
    mockFetch([
      ...detailRoutes(detail).map((route) =>
        route.path === '/api/v1/actions/act-1' ? { ...route, handler: () => jsonResponse(detail) } : route,
      ),
      {
        method: 'PATCH',
        path: '/api/v1/actions/act-1',
        handler: () => {
          detail = {
            ...detail,
            status: 'pending_approval',
            version: 4,
            payload_hash: 'f'.repeat(64),
            current_approval: { ...current, invalidated_at: '2026-10-24T10:10:00Z' },
          };
          return jsonResponse(detail);
        },
      },
    ]);
    renderWithProviders(<ActionCard actionId="act-1" />);
    const title = await screen.findByRole('textbox', { name: /^Title/ });
    await user.type(title, ' (EU)');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    const notice = await screen.findByTestId('save-notice');
    expectIsolated(
      within(notice).getByText(
        (_, el) => el?.tagName === 'P' && /previous approval by/.test(el.textContent ?? ''),
      ),
      shown,
    );
    expect(notice).toHaveTextContent(`The previous approval by ${shown} (v3) was invalidated`);
  });

  it('Email headers: sender and recipient names before their addresses', () => {
    renderWithProviders(
      <EmailHeaders
        email={emailDetail({
          sender_name: RLO,
          sender_address: 'dana@fabrikam.example',
          to_recipients: [
            { name: RLO, address: 'support@contoso.example' },
            { name: null, address: 'ops@contoso.example' },
          ],
        })}
      />,
    );
    const from = sentenceMatching(/<dana@fabrikam\.example>$/);
    expectIsolated(from, RLO);
    expect(from.textContent).toBe(`${RLO} <dana@fabrikam.example>`);
    const to = sentenceMatching(/<support@contoso\.example>, ops@contoso\.example$/);
    expectIsolated(to, RLO);
    expect(to.textContent).toBe(`${RLO} <support@contoso.example>, ops@contoso.example`);
  });

  it('AI analysis: the extracted requester name before the requester email', () => {
    renderWithProviders(
      <AnalysisPanel
        analysis={analysis({
          extraction: {
            ...analysis().extraction,
            requester_name: RLO,
            requester_email: 'dana@fabrikam.example',
          },
        })}
      />,
    );
    const requester = sentenceMatching(/ · dana@fabrikam\.example$/);
    expectIsolated(requester, RLO);
    expect(requester.textContent).toBe(`${RLO} · dana@fabrikam.example`);
  });

  it('Overview activity feed: the actor name before the resource type and time', () => {
    renderWithProviders(<ActivityFeed events={[auditEvent({ actor_name: RLO })]} />);
    expectIsolated(
      screen.getByText((_, el) => el?.tagName === 'P' && (el.textContent ?? '').startsWith(`${RLO} · `)),
      RLO,
    );
  });

  it('Reply draft: recipients before the creation time', async () => {
    mockFetch([
      { method: 'GET', path: '/api/v1/tasks', handler: () => jsonResponse(page([])) },
      {
        method: 'GET',
        path: '/api/v1/reply-drafts',
        handler: () =>
          jsonResponse(page([replyDraftFixture({ to_recipients: [RLO, 'ops@contoso.example'] })])),
      },
    ]);
    renderWithProviders(<TasksPage />, { route: '/?tab=drafts' });
    const line = await screen.findByText(
      (_, el) => el?.tagName === 'P' && (el.textContent ?? '').startsWith('To '),
    );
    expectIsolated(line, RLO);
  });

  it('Integrations: mailbox address before the provider, sync folder before the count, organization in the test result', async () => {
    const user = userEvent.setup();
    const base = integrationsOut();
    const [demo] = base.microsoft.connections;
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/integrations',
        handler: () =>
          jsonResponse(
            integrationsOut({
              microsoft: {
                ...base.microsoft,
                connections: [{ ...demo!, email: RLO, sync: { ...demo!.sync!, folder: RLO } }],
              },
            }),
          ),
      },
      {
        method: 'GET',
        path: '/api/v1/integrations/azure-devops/test',
        handler: () =>
          jsonResponse({ ok: true, mode: 'mock', organization: RLO, message: 'OK', latency_ms: 12 }),
      },
      {
        method: 'GET',
        path: '/api/v1/health/ready',
        handler: () => jsonResponse({ status: 'ok', version: '1', checks: {} }),
      },
      { method: 'GET', path: '/api/v1/auth/config', handler: () => jsonResponse(authConfig()) },
    ]);
    renderWithProviders(<IntegrationsPage />, { withNotifications: true });
    await screen.findByRole('region', { name: 'Microsoft 365 mailbox' });
    expectIsolated(sentenceMatching(/ · Demo mailbox$/), RLO);
    expectIsolated(sentenceMatching(/ · 42 ingested$/), RLO);
    await user.click(screen.getByRole('button', { name: 'Test connection' }));
    expectIsolated(
      await screen.findByText(
        (_, el) => el?.tagName === 'P' && /^Mode mock · organization /.test(el.textContent ?? ''),
      ),
      RLO,
    );
  });

  it('PlainText inline (approval comment, expected impact) is an isolate', () => {
    const current = approvalFixture({ reason: RLO });
    renderWithProviders(
      <CurrentApproval action={adoAction({ status: 'approved', current_approval: current })} />,
    );
    expectIsolated(sentenceMatching(/^Comment: /), RLO);
  });
});
