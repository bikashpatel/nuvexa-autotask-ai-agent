import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { AnalysisOut } from '../../api/types';
import { formatDateTime } from '../../lib/format';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { XSS_IMG, XSS_SCRIPT, XSS_TEXT, analysis } from '../../test/fixtures';
import { renderWithProviders } from '../../test/utils';
import { AnalysisPanel } from './AnalysisPanel';

/** The <dd> value next to a FactList <dt> label. */
function fact(label: string): HTMLElement {
  const term = screen.getByText(label, { selector: 'dt' });
  return term.nextElementSibling as HTMLElement;
}

function renderPanel(overrides: Partial<AnalysisOut> = {}) {
  return renderWithProviders(<AnalysisPanel analysis={analysis(overrides)} />);
}

const FULL_EXTRACTION = {
  summary: 'Fix checkout 500 errors for EU customers',
  requester_name: 'Dana Customer',
  requester_email: 'dana@fabrikam.example',
  organization: 'Fabrikam',
  product_or_system: 'Checkout API',
  environment: 'production',
  impact: 'All EU orders fail',
  deadline: '2026-11-02T09:30:00Z',
  suggested_owner: 'Payments on-call',
  suggested_team: 'Payments',
  suggested_work_item_type: 'Bug',
  reference_urls: ['https://status.fabrikam.example/incident/42', 'javascript:alert(1)'],
  reference_ids: ['INC-42', 'INC-42'],
  references: ['mock://contoso/ref'],
  evidence: [
    { field: 'impact', quote: 'Checkout returns HTTP 500.' },
    { field: 'requester_email', quote: 'dana@fabrikam.example' },
  ],
};

describe('<AnalysisPanel>', () => {
  it('renders the header, badges, confidence and rationale', async () => {
    renderPanel({
      classification: { rationale: 'Production checkout outage reported.' },
      model_provider: 'openai',
      model_name: 'gpt-x',
      prompt_version: 'p7',
      attempt: 2,
    });
    const section = screen.getByRole('region', { name: 'AI analysis' });
    expect(section).toHaveTextContent('Attempt #2 · openai gpt-x · prompt p7');
    expect(within(section).getByText('Incident')).toBeInTheDocument();
    expect(screen.getByLabelText('Priority: High')).toHaveTextContent('High');
    expect(screen.getByLabelText('Urgency: Immediate')).toHaveTextContent('Urgency: Immediate');
    expect(screen.getByText('Action recommended')).toBeInTheDocument();
    expect(screen.queryByText('Needs more info')).toBeNull();
    expect(screen.getByRole('progressbar', { name: /Heuristic confidence 0\.62 \(Medium confidence\)/ }));
    expect(screen.getByText('Production checkout outage reported.')).toBeInTheDocument();
    expect(screen.getByText(/Schema s1 · analyzed/)).toHaveTextContent(
      formatDateTime('2026-10-24T10:00:00Z'),
    );
    await expectNoSeriousA11yViolations();
  });

  it('shows "No action recommended" and "Needs more info" when flagged', () => {
    renderPanel({ action_recommended: false, needs_more_info: true });
    expect(screen.getByText('No action recommended')).toBeInTheDocument();
    expect(screen.getByText('Needs more info')).toBeInTheDocument();
  });

  it('renders every extracted field, evidence quotes and unique references as text only', () => {
    const { container } = renderPanel({ extraction: FULL_EXTRACTION });
    expect(screen.getByText('Summary')).toBeInTheDocument();
    expect(screen.getByText('Fix checkout 500 errors for EU customers')).toBeInTheDocument();
    expect(fact('Requester')).toHaveTextContent('Dana Customer · dana@fabrikam.example');
    expect(fact('Organization')).toHaveTextContent('Fabrikam');
    expect(fact('Product / system')).toHaveTextContent('Checkout API');
    expect(fact('Environment')).toHaveTextContent('production');
    expect(fact('Impact')).toHaveTextContent('All EU orders fail');
    expect(fact('Deadline')).toHaveTextContent(formatDateTime('2026-11-02T09:30:00Z'));
    expect(fact('Owner')).toHaveTextContent('Payments on-call');
    expect(fact('Team')).toHaveTextContent('Payments');
    expect(fact('Suggested type')).toHaveTextContent('Bug');

    const refs = fact('References');
    expect(within(refs).getByText('https://status.fabrikam.example/incident/42')).toBeInTheDocument();
    expect(within(refs).getByText('javascript:alert(1)')).toBeInTheDocument();
    expect(within(refs).getByText('mock://contoso/ref')).toBeInTheDocument();
    expect(within(refs).getAllByText('INC-42')).toHaveLength(1);
    expect(within(refs).getByText(/not clickable/)).toBeInTheDocument();
    // References copied from the email are never links.
    expect(refs.querySelector('a')).toBeNull();
    expect(container.querySelector('a')).toBeNull();

    const quotes = container.querySelectorAll('blockquote');
    expect(quotes).toHaveLength(2);
    expect(quotes[0]).toHaveTextContent('Checkout returns HTTP 500.');
    expect(quotes[0]).toHaveTextContent('— supports Impact');
    expect(quotes[1]).toHaveTextContent('— supports Requester email');
  });

  it('falls back to "—" for every missing field and hides the suggested type', () => {
    renderPanel({ extraction: {}, classification: {} });
    for (const label of [
      'Requester',
      'Organization',
      'Product / system',
      'Environment',
      'Impact',
      'Deadline',
      'Owner',
      'Team',
      'References',
    ]) {
      expect(fact(label)).toHaveTextContent('—');
    }
    expect(screen.queryByText('Suggested type', { selector: 'dt' })).toBeNull();
    expect(screen.queryByText('Summary')).toBeNull();
    expect(screen.queryByText('Evidence')).toBeNull();
    expect(screen.queryByText('Missing information')).toBeNull();
    expect(screen.queryByText('Validation warnings')).toBeNull();
    // Rationale fallback
    expect(screen.getByText('Rationale').nextElementSibling).toHaveTextContent('—');
  });

  it('joins only the requester parts that exist', () => {
    renderPanel({ extraction: { requester_email: 'dana@fabrikam.example' } });
    expect(fact('Requester')).toHaveTextContent(/^dana@fabrikam\.example$/);
  });

  it('lists missing information and de-duplicates validation + extraction warnings', () => {
    renderPanel({
      validation_warnings: ['Deadline not stated', 'Requester unknown'],
      classification: {
        missing_information: ['affected environment', 'order id'],
        extraction_warnings: ['Deadline not stated', 'Low evidence for impact'],
      },
    });
    const missing = screen.getByRole('alert', { name: /Missing information/ });
    expect(
      within(missing)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['affected environment', 'order id']);
    const warnings = screen.getByRole('alert', { name: /Validation warnings/ });
    expect(
      within(warnings)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['Deadline not stated', 'Requester unknown', 'Low evidence for impact']);
  });

  it('reads malformed (open dictionary) classification/extraction defensively', () => {
    const malformed = analysis({
      classification: {
        rationale: 42,
        missing_information: 'not-a-list',
        extraction_warnings: [null, '', '  ', 'kept'],
      } as unknown as AnalysisOut['classification'],
      extraction: {
        summary: '   ',
        organization: 42,
        requester_name: ['x'],
        impact: { nested: true },
        reference_urls: 'https://not-a-list.example',
        reference_ids: [1, null, 'REF-1'],
        evidence: [null, { field: 'impact', quote: 7 }, { field: 'team', quote: 'kept quote' }],
      } as unknown as AnalysisOut['extraction'],
    });
    const { container } = renderWithProviders(<AnalysisPanel analysis={malformed} />);
    expect(screen.getByText('Rationale').nextElementSibling).toHaveTextContent('—');
    expect(screen.queryByText('Summary')).toBeNull();
    expect(screen.queryByText('Missing information')).toBeNull();
    expect(fact('Organization')).toHaveTextContent('—');
    expect(fact('Requester')).toHaveTextContent('—');
    expect(fact('Impact')).toHaveTextContent('—');
    expect(within(fact('References')).getByText('REF-1')).toBeInTheDocument();
    expect(fact('References')).not.toHaveTextContent('not-a-list');
    expect(container.querySelectorAll('blockquote')).toHaveLength(1);
    expect(screen.getByText('kept quote')).toBeInTheDocument();
    expect(within(screen.getByRole('alert', { name: /Validation warnings/ })).getByText('kept'));
  });

  it('tolerates null classification/extraction/evidence containers', () => {
    renderWithProviders(
      <AnalysisPanel
        analysis={analysis({
          classification: null as unknown as AnalysisOut['classification'],
          extraction: null as unknown as AnalysisOut['extraction'],
        })}
      />,
    );
    expect(fact('Organization')).toHaveTextContent('—');
  });

  it('renders untrusted model output (rationale, summary, impact, evidence, references) as inert text', () => {
    const { container } = renderPanel({
      classification: { rationale: XSS_TEXT, missing_information: [XSS_IMG] },
      validation_warnings: [XSS_SCRIPT],
      extraction: {
        summary: XSS_TEXT,
        impact: XSS_IMG,
        organization: XSS_SCRIPT,
        reference_urls: [XSS_IMG],
        evidence: [{ field: 'impact', quote: XSS_SCRIPT }],
      },
    });
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getAllByText(XSS_TEXT)).toHaveLength(2);
    expect(screen.getAllByText(XSS_IMG).length).toBeGreaterThanOrEqual(3);
    expect(screen.getAllByText(XSS_SCRIPT).length).toBeGreaterThanOrEqual(3);
    expect((window as unknown as { __nuvexaXss?: number }).__nuvexaXss).toBeUndefined();
  });
});
