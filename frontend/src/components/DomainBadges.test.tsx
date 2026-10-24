import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { EMAIL_CATEGORIES, PRIORITIES, URGENCIES, type Urgency } from '../api/types';
import { CATEGORY_LABELS, PRIORITY_LABELS, URGENCY_LABELS } from '../lib/format';
import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { CategoryBadge, MockBadge, PriorityBadge, UrgencyBadge } from './DomainBadges';

function badgeStyle(el: Element): string {
  return el.closest('.mantine-Badge-root')?.getAttribute('style') ?? '';
}

describe('<CategoryBadge>', () => {
  it.each(EMAIL_CATEGORIES)('renders %s with its label and a decorative icon', (category) => {
    renderWithProviders(<CategoryBadge category={category} />);
    const label = screen.getByText(CATEGORY_LABELS[category]);
    const badge = label.closest('.mantine-Badge-root')!;
    expect(badge.getAttribute('data-variant')).toBe('default');
    const icon = badge.querySelector('svg');
    expect(icon).not.toBeNull();
    expect(icon).toHaveAttribute('aria-hidden', 'true');
  });

  it.each([null, undefined, ''])('shows "Unclassified" for %s', (value) => {
    renderWithProviders(<CategoryBadge category={value} />);
    const badge = screen.getByText('Unclassified').closest('.mantine-Badge-root')!;
    expect(badge.getAttribute('data-variant')).toBe('outline');
    expect(badge.querySelector('svg')).toBeNull();
  });

  it('humanizes an unknown (forward-compatible) category and uses the generic icon', () => {
    renderWithProviders(<CategoryBadge category="security_report" />);
    const badge = screen.getByText('Security report').closest('.mantine-Badge-root')!;
    expect(badge.querySelector('svg')).not.toBeNull();
  });

  it('forwards Badge props', () => {
    renderWithProviders(<CategoryBadge category="bug" size="lg" data-testid="cat" />);
    expect(screen.getByTestId('cat')).toHaveAttribute('data-size', 'lg');
  });
});

describe('<PriorityBadge>', () => {
  const colors = { critical: 'red', high: 'orange', medium: 'yellow', low: 'gray' } as const;

  it.each(PRIORITIES)('renders %s with an accessible label, colour and icon', (priority) => {
    renderWithProviders(<PriorityBadge priority={priority} />);
    const badge = screen.getByLabelText(`Priority: ${PRIORITY_LABELS[priority]}`);
    expect(badge).toHaveTextContent(PRIORITY_LABELS[priority]);
    expect(badgeStyle(badge)).toContain(`--mantine-color-${colors[priority]}-light`);
    expect(badge.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  });

  it.each([null, undefined, ''])('renders nothing for %s', (value) => {
    const { container } = renderWithProviders(<PriorityBadge priority={value} />);
    expect(container.querySelector('.mantine-Badge-root')).toBeNull();
  });

  it('renders an unknown priority neutrally', () => {
    renderWithProviders(<PriorityBadge priority="urgent_plus" />);
    const badge = screen.getByLabelText('Priority: Urgent plus');
    expect(badgeStyle(badge)).toContain('--mantine-color-gray-light');
    expect(badge.querySelector('svg')).not.toBeNull();
  });
});

describe('<UrgencyBadge>', () => {
  const colors = { immediate: 'red', high: 'orange', normal: 'gray', low: 'gray' } as const;

  it.each(URGENCIES)('renders %s', (urgency) => {
    renderWithProviders(<UrgencyBadge urgency={urgency} />);
    const badge = screen.getByLabelText(`Urgency: ${URGENCY_LABELS[urgency]}`);
    expect(badge).toHaveTextContent(`Urgency: ${URGENCY_LABELS[urgency]}`);
    expect(badge.getAttribute('data-variant')).toBe('outline');
    expect(badge.getAttribute('style')).toContain(`--mantine-color-${colors[urgency]}-outline`);
  });

  it.each([null, undefined])('renders nothing for %s', (value) => {
    const { container } = renderWithProviders(<UrgencyBadge urgency={value} />);
    expect(container.querySelector('.mantine-Badge-root')).toBeNull();
  });

  it('labels an unknown urgency (regression: aria-label read "Urgency: undefined")', () => {
    renderWithProviders(<UrgencyBadge urgency={'next_week' as Urgency} />);
    const badge = screen.getByLabelText('Urgency: Next week');
    expect(badge).toHaveTextContent('Urgency: Next week');
    expect(screen.queryByLabelText(/undefined/)).toBeNull();
  });
});

describe('<MockBadge>', () => {
  it('renders a small filled "Mock" badge and forwards props', async () => {
    renderWithProviders(<MockBadge data-testid="mock" />);
    const badge = screen.getByTestId('mock');
    expect(badge).toHaveTextContent('Mock');
    expect(badge).toHaveAttribute('data-variant', 'filled');
    expect(badge).toHaveAttribute('data-size', 'xs');
    await expectNoSeriousA11yViolations();
  });
});

describe('domain badges together', () => {
  it('have no serious accessibility violations', async () => {
    renderWithProviders(
      <div>
        <CategoryBadge category="incident" />
        <CategoryBadge category={null} />
        <PriorityBadge priority="critical" />
        <UrgencyBadge urgency="immediate" />
      </div>,
    );
    await expectNoSeriousA11yViolations();
  });
});
