import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { ConfidenceBucket } from '../api/types';
import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { ConfidenceIndicator } from './ConfidenceIndicator';

const TOOLTIP = 'Heuristic estimate, not a calibrated probability; method heuristic-v1.';

describe('<ConfidenceIndicator> (full)', () => {
  it('shows the score, bucket label, an accessible progressbar and an info button', async () => {
    renderWithProviders(<ConfidenceIndicator score={0.62} bucket="medium" method="heuristic-v1" />);
    expect(screen.getByText('0.62')).toBeInTheDocument();
    expect(screen.getByText('Medium confidence')).toBeInTheDocument();
    const bar = screen.getByRole('progressbar', { name: 'Heuristic confidence 0.62 (Medium confidence)' });
    expect(bar).toHaveAttribute('aria-valuenow', '62');
    expect(screen.getByRole('button', { name: `About this score: ${TOOLTIP}` })).toBeInTheDocument();
    expect(screen.getByText('Heuristic estimate · heuristic-v1')).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
  });

  it.each([
    ['high', 'High confidence', 'teal'],
    ['medium', 'Medium confidence', 'yellow'],
    ['low', 'Low confidence', 'red'],
  ] as const)('maps bucket %s to "%s" (%s)', (bucket, label, color) => {
    renderWithProviders(<ConfidenceIndicator score={0.5} bucket={bucket} />);
    const badge = screen.getByText(label).closest('.mantine-Badge-root')!;
    expect(badge.getAttribute('style')).toContain(`--mantine-color-${color}-light`);
    expect(
      screen.getByRole('progressbar', { name: `Heuristic confidence 0.50 (${label})` }),
    ).toBeInTheDocument();
  });

  it('falls back to the raw bucket and gray for an unknown bucket', () => {
    renderWithProviders(<ConfidenceIndicator score={0.5} bucket={'experimental' as ConfidenceBucket} />);
    const badge = screen.getByText('experimental').closest('.mantine-Badge-root')!;
    expect(badge.getAttribute('style')).toContain('--mantine-color-gray-light');
  });

  it.each([
    [-0.3, '0.00', '0'],
    [1.7, '1.00', '100'],
    [Number.NaN, '0.00', '0'],
    [Number.POSITIVE_INFINITY, '0.00', '0'],
    [0.999, '1.00', '99.9'],
  ])('clamps %s to %s', (score, text, valueNow) => {
    renderWithProviders(<ConfidenceIndicator score={score} bucket="low" />);
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', valueNow);
  });

  it('uses the given method and defaults to heuristic-v1 when empty', () => {
    const { unmount } = renderWithProviders(
      <ConfidenceIndicator score={0.9} bucket="high" method="heuristic-v2" />,
    );
    expect(screen.getByText('Heuristic estimate · heuristic-v2')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /method heuristic-v2\.$/ })).toBeInTheDocument();
    unmount();
    renderWithProviders(<ConfidenceIndicator score={0.9} bucket="high" method="" />);
    expect(screen.getByText('Heuristic estimate · heuristic-v1')).toBeInTheDocument();
  });

  it('shows the explanation tooltip when the info button is hovered or focused', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfidenceIndicator score={0.4} bucket="low" />);
    await user.hover(screen.getByRole('button', { name: /About this score/ }));
    expect(await screen.findByRole('tooltip')).toHaveTextContent(TOOLTIP);
  });
});

describe('<ConfidenceIndicator compact>', () => {
  it('renders a single badge with score · bucket and a descriptive label', async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConfidenceIndicator compact score={0.62} bucket="medium" method={null} />);
    const badge = screen.getByLabelText(`Medium confidence, heuristic score 0.62. ${TOOLTIP}`);
    expect(badge).toHaveTextContent('0.62 · medium');
    expect(screen.queryByRole('progressbar')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
    await user.hover(badge);
    expect(await screen.findByRole('tooltip')).toHaveTextContent(TOOLTIP);
  });

  it('clamps in compact mode too', () => {
    renderWithProviders(<ConfidenceIndicator compact score={4} bucket="high" />);
    expect(screen.getByText('1.00 · high')).toBeInTheDocument();
  });
});
