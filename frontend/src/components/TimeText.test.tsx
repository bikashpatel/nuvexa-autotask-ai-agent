import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { formatDateTime } from '../lib/format';
import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { TimeText } from './TimeText';

const VALUE = '2026-10-24T10:00:00Z';

beforeEach(() => {
  // Only Date is faked: Mantine's tooltip delays and axe keep using real timers.
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-10-24T12:00:00Z'));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('<TimeText>', () => {
  it.each([null, undefined, ''])('renders a dimmed dash for %j', (value) => {
    const { container } = renderWithProviders(<TimeText value={value} />);
    expect(screen.getByText('—')).toBeInTheDocument();
    expect(container.querySelector('time')).toBeNull();
  });

  it('renders relative time in a <time> element carrying the machine-readable value', async () => {
    renderWithProviders(<TimeText value={VALUE} />);
    const time = screen.getByText('2 hours ago');
    expect(time.tagName).toBe('TIME');
    expect(time).toHaveAttribute('datetime', VALUE);
    await expectNoSeriousA11yViolations();
  });

  it('renders future times relatively too', () => {
    renderWithProviders(<TimeText value="2026-10-26T12:00:00Z" />);
    expect(screen.getByText('in 2 days')).toBeInTheDocument();
  });

  it('renders the absolute local date-time in absolute mode', () => {
    renderWithProviders(<TimeText value={VALUE} mode="absolute" />);
    const time = screen.getByText(formatDateTime(VALUE));
    expect(time.tagName).toBe('TIME');
    expect(time.textContent).toMatch(/^Oct 2[45], 2026 \d{2}:\d{2}$/);
  });

  it('shows the absolute local time and the UTC timestamp in a tooltip on hover', async () => {
    const user = userEvent.setup();
    renderWithProviders(<TimeText value={VALUE} />);
    await user.hover(screen.getByText('2 hours ago'));
    expect(await screen.findByRole('tooltip')).toHaveTextContent(
      `${formatDateTime(VALUE)} · 2026-10-24 10:00:00 UTC`,
    );
  });

  it('shows dashes for an unparseable value instead of "Invalid Date"', () => {
    renderWithProviders(<TimeText value="not-a-date" />);
    const time = screen.getByText('—');
    expect(time.tagName).toBe('TIME');
    expect(screen.queryByText(/Invalid/)).toBeNull();
  });

  it('forwards Text props', () => {
    renderWithProviders(<TimeText value={VALUE} size="xs" data-testid="t" />);
    expect(screen.getByTestId('t')).toHaveAttribute('data-size', 'xs');
  });
});
