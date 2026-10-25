/**
 * Regression (Prompt 5): enum-keyed label/icon/colour maps are looked up by own key only.
 *
 * API enum values are open strings to the browser (forward-compatible additions, or a hostile value).
 * A plain `MAP[value] ?? fallback` resolves inherited names — "constructor" is `Object`, "__proto__"
 * is `Object.prototype`, "toString" is a function — so a badge or icon lookup would render a function
 * or object and crash the page. Every lookup falls back exactly like an unknown value.
 */
import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActionDetail, WorkflowDetail } from '../api/types';
import { ConfidenceIndicator } from '../components/ConfidenceIndicator';
import { CategoryBadge, PriorityBadge, UrgencyBadge } from '../components/DomainBadges';
import { InjectionWarning } from '../components/InjectionWarning';
import { ActionCard } from '../features/actions/ActionCard';
import { ApprovalsPage } from '../features/approvals/ApprovalsPage';
import { WorkflowDetailPage } from '../features/workflows/WorkflowDetailPage';
import { computeStepperState } from '../features/workflows/workflowSteps';
import { actionTypeLabel, categoryLabel, humanize, priorityLabel } from '../lib/format';
import { isOwnKey, lookup } from '../lib/lookup';
import {
  actionSummary,
  adoAction,
  analysis,
  page,
  workflowDetail,
  workflowEvent,
  workflowRun,
} from './fixtures';
import { jsonResponse, mockFetch, renderWithProviders } from './utils';

const INHERITED = ['constructor', '__proto__', 'toString', 'hasOwnProperty', 'valueOf'];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('lookup()', () => {
  const MAP: Record<string, number> = { a: 1, b: 2 };

  const NUMERIC: Record<number, string> = { 1: 'one' };

  it('returns own values', () => {
    expect(lookup(MAP, 'a')).toBe(1);
    expect(lookup(NUMERIC, 1)).toBe('one');
    expect(isOwnKey(MAP, 'b')).toBe(true);
  });

  it.each([...INHERITED, 'unknown', '', null, undefined])('returns undefined for %j', (key) => {
    expect(lookup(MAP, key)).toBeUndefined();
    expect(isOwnKey(MAP, key)).toBe(false);
  });
});

describe('label helpers', () => {
  it.each(INHERITED)('fall back to the humanized value for %j', (value) => {
    for (const label of [categoryLabel(value), priorityLabel(value), actionTypeLabel(value)]) {
      expect(typeof label).toBe('string');
      expect(label).toBe(humanize(value));
    }
  });
});

describe('domain badges', () => {
  it.each(INHERITED)('render %j as a neutral, humanized badge', (value) => {
    renderWithProviders(
      <>
        <CategoryBadge category={value} />
        <PriorityBadge priority={value} />
        <UrgencyBadge urgency={value as never} />
      </>,
    );
    const label = humanize(value);
    expect(screen.getAllByText(label).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByLabelText(`Priority: ${label}`)).toBeInTheDocument();
    expect(screen.getByLabelText(`Urgency: ${label}`)).toHaveTextContent(`Urgency: ${label}`);
  });
});

describe('confidence and injection indicators', () => {
  it.each(INHERITED)('ConfidenceIndicator with bucket %j shows the raw bucket in gray', (value) => {
    renderWithProviders(<ConfidenceIndicator score={0.5} bucket={value as never} />);
    const badge = screen.getByText(value).closest('.mantine-Badge-root')!;
    expect(badge.getAttribute('style')).toContain('--mantine-color-gray-light');
    expect(
      screen.getByRole('progressbar', { name: `Heuristic confidence 0.50 (${value})` }),
    ).toBeInTheDocument();
  });

  it.each(INHERITED)('InjectionWarning with severity %j still renders the flag', (value) => {
    renderWithProviders(
      <InjectionWarning
        flags={[{ rule: 'ignore_previous_instructions', severity: value as never, excerpt: 'x' }]}
      />,
    );
    expect(screen.getByTestId('injection-warning')).toHaveTextContent(value);
  });
});

describe('action card header', () => {
  it.each(INHERITED)('action_type and target_integration %j render with fallbacks', async (value) => {
    const action: ActionDetail = adoAction({
      action_type: value as never,
      target_integration: value as never,
    });
    mockFetch([
      { method: 'GET', path: '/api/v1/actions/act-1', handler: () => jsonResponse(action) },
      { method: 'GET', path: '/api/v1/actions/act-1/history', handler: () => jsonResponse([]) },
    ]);
    renderWithProviders(<ActionCard actionId="act-1" />);
    expect(await screen.findByRole('heading', { level: 3, name: action.title })).toBeInTheDocument();
    expect(screen.getByText(`${humanize(value)} · ${value}`)).toBeInTheDocument();
    expect(screen.getByText('Unsupported action type.')).toBeInTheDocument();
  });
});

describe('approvals list', () => {
  it.each(INHERITED)('an action_type of %j is shown as its raw value', async (value) => {
    mockFetch([
      {
        method: 'GET',
        path: '/api/v1/actions',
        handler: () => jsonResponse(page([actionSummary({ action_type: value as never })])),
      },
      {
        method: 'GET',
        path: /^\/api\/v1\/workflows\/[^/]+\/analysis$/,
        handler: () => jsonResponse(analysis()),
      },
    ]);
    renderWithProviders(<ApprovalsPage />);
    expect(await screen.findByText(value)).toBeInTheDocument();
  });
});

describe('workflow detail', () => {
  it.each(INHERITED)('an event_type and current_step of %j render the timeline', async (value) => {
    const detail: WorkflowDetail = workflowDetail({
      run: workflowRun({ status: 'failed', current_step: value }),
      events: [workflowEvent({ event_type: value as never, step: value })],
    });
    mockFetch([
      { method: 'GET', path: '/api/v1/workflows/run-1', handler: () => jsonResponse(detail) },
      { method: 'GET', path: /^\/api\/v1\/actions\/[^/]+\/history$/, handler: () => jsonResponse([]) },
    ]);
    renderWithProviders(<WorkflowDetailPage />, {
      path: '/workflows/:workflowId',
      route: '/workflows/run-1',
    });
    expect(await screen.findAllByText(humanize(value))).not.toHaveLength(0);
  });

  it.each(INHERITED)('computeStepperState ignores a current_step / failed step of %j', (value) => {
    const failed = computeStepperState(workflowRun({ status: 'failed', current_step: value }), [
      workflowEvent({ event_type: 'step_failed', step: value }),
    ]);
    expect(failed).toEqual({ active: 0, loadingIndex: null, stoppedIndex: 0, terminal: 'failed' });
    const cancelled = computeStepperState(workflowRun({ status: 'cancelled', current_step: value }));
    expect(cancelled).toEqual({ active: 6, loadingIndex: null, stoppedIndex: 6, terminal: 'cancelled' });
  });
});
