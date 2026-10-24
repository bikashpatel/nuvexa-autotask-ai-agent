import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { STATUS_MAPS, type StatusDescriptor, type StatusKind } from '../theme/status';
import { StatusBadge } from './StatusBadge';

const KINDS = Object.keys(STATUS_MAPS) as StatusKind[];

function entries(kind: StatusKind): [string, StatusDescriptor][] {
  return Object.entries(STATUS_MAPS[kind] as Record<string, StatusDescriptor>);
}

function badgeFor(container: HTMLElement, status: string): HTMLElement {
  const el = container.querySelector<HTMLElement>(`[data-status="${status}"]`);
  if (!el) throw new Error(`No badge for ${status}`);
  return el;
}

describe('<StatusBadge>', () => {
  it.each(KINDS)('renders every %s status with label, tone, colour and the right icon', async (kind) => {
    const list = entries(kind);
    const { container } = renderWithProviders(
      <div>
        {list.map(([status]) => (
          <StatusBadge key={status} kind={kind} status={status} />
        ))}
      </div>,
    );
    for (const [status, descriptor] of list) {
      const badge = badgeFor(container, status);
      expect(badge).toHaveAttribute('aria-label', `Status: ${descriptor.label}`);
      expect(badge).toHaveTextContent(descriptor.label);
      expect(badge).toHaveAttribute('data-tone', descriptor.tone);
      expect(badge).toHaveAttribute('data-variant', descriptor.variant);
      // The "default" variant (superseded) is neutral by design and ignores the colour.
      if (descriptor.variant !== 'default') {
        expect(badge.getAttribute('style') ?? '').toContain(`--mantine-color-${descriptor.color}`);
      }
      const loader = badge.querySelector('.mantine-Loader-root');
      if (descriptor.loading) {
        expect(loader).not.toBeNull();
        expect(loader).toHaveAttribute('aria-hidden', 'true');
        expect(badge.querySelector('svg')).toBeNull();
      } else {
        expect(loader).toBeNull();
        expect(badge.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
      }
      if (descriptor.dimmed) expect(badge.style.opacity).toBe('0.6');
      else expect(badge.style.opacity).toBe('');
    }
    await expectNoSeriousA11yViolations();
  });

  it('is queryable by its accessible name', () => {
    renderWithProviders(<StatusBadge kind="action" status="pending_approval" />);
    expect(screen.getByLabelText('Status: Pending approval')).toHaveTextContent('Pending approval');
  });

  it('dims superseded proposals', () => {
    renderWithProviders(<StatusBadge kind="action" status="superseded" />);
    expect(screen.getByLabelText('Status: Superseded').style.opacity).toBe('0.6');
  });

  it('renders unknown statuses neutrally with a humanized label', () => {
    renderWithProviders(<StatusBadge kind="workflow" status="awaiting_human.review" />);
    const badge = screen.getByLabelText('Status: Awaiting human review');
    expect(badge).toHaveAttribute('data-tone', 'neutral');
    expect(badge).toHaveAttribute('data-status', 'awaiting_human.review');
    expect(badge.getAttribute('style')).toContain('--mantine-color-gray');
  });

  it('shows the description in a tooltip on hover when withTooltip is set', async () => {
    const user = userEvent.setup();
    renderWithProviders(<StatusBadge kind="action" status="uncertain" withTooltip />);
    const badge = screen.getByLabelText('Status: Uncertain');
    await user.hover(badge);
    expect(await screen.findByRole('tooltip')).toHaveTextContent(
      'The outcome of the external write is unknown. Reconcile before retrying.',
    );
    await user.unhover(badge);
    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull());
  });

  it('does not add a tooltip without withTooltip, or when the status has no description', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <div>
        <StatusBadge kind="action" status="approved" />
        <StatusBadge kind="workflow" status="received" withTooltip />
      </div>,
    );
    await user.hover(screen.getByLabelText('Status: Approved'));
    await user.hover(screen.getByLabelText('Status: Received'));
    // Give an (incorrect) tooltip time to open past the theme's 150 ms delay.
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('forwards size and other Badge props', () => {
    renderWithProviders(<StatusBadge kind="task" status="open" size="xs" data-testid="task-status" />);
    const badge = screen.getByTestId('task-status');
    expect(badge).toHaveAttribute('data-size', 'xs');
    expect(badge).toHaveAttribute('aria-label', 'Status: Open');
  });

  it('defaults to size md', () => {
    renderWithProviders(<StatusBadge kind="sync" status="idle" />);
    expect(screen.getByLabelText('Status: Idle')).toHaveAttribute('data-size', 'md');
  });
});
