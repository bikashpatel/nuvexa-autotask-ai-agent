import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { DistributionBars, type DistributionItem } from './DistributionBars';

const items: DistributionItem[] = [
  { key: 'bug', label: <strong>Bug</strong>, text: 'Bug', value: 9 },
  { key: 'incident', label: 'Incident', text: 'Incident', value: 6, color: 'var(--mantine-color-red-5)' },
  { key: 'info', label: 'Information only', text: 'Information only', value: 10_000 },
  { key: 'other', label: 'Other', text: 'Other', value: 0 },
];

function fill(item: HTMLElement): HTMLElement {
  return item.querySelector<HTMLElement>('.nuvexa-bar-fill')!;
}

describe('<DistributionBars>', () => {
  it.each([
    ['no items', []],
    ['only zero values', [{ key: 'a', label: 'A', text: 'A', value: 0 }]],
  ])('shows the empty text for %s', (_, list: DistributionItem[]) => {
    renderWithProviders(<DistributionBars items={list} ariaLabel="Categories" emptyText="Nothing yet." />);
    expect(screen.getByText('Nothing yet.')).toBeInTheDocument();
    expect(screen.queryByRole('list')).toBeNull();
  });

  it('defaults the empty text', () => {
    renderWithProviders(<DistributionBars items={[]} ariaLabel="Categories" />);
    expect(screen.getByText('No data yet.')).toBeInTheDocument();
  });

  it('renders a named list whose items carry label, value and percentage as text', async () => {
    renderWithProviders(<DistributionBars items={items} ariaLabel="Email categories" />);
    const list = screen.getByRole('list', { name: 'Email categories' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows).toHaveLength(4);
    // total = 10_015
    expect(rows[0]).toHaveAccessibleName('Bug: 9 (0%)');
    expect(rows[1]).toHaveAccessibleName('Incident: 6 (0%)');
    expect(rows[2]).toHaveAccessibleName('Information only: 10000 (100%)');
    expect(rows[3]).toHaveAccessibleName('Other: 0 (0%)');
    expect(within(rows[2]!).getByText('10,000')).toBeInTheDocument();
    expect(within(rows[0]!).getByText('Bug').tagName).toBe('STRONG');
    await expectNoSeriousA11yViolations();
  });

  it('scales bars to the largest value with a 2% minimum for non-zero values and 0% for zero', () => {
    renderWithProviders(<DistributionBars items={items} ariaLabel="Email categories" />);
    const rows = screen.getAllByRole('listitem');
    expect(fill(rows[2]!).style.width).toBe('100%');
    expect(fill(rows[0]!).style.width).toBe('2%');
    expect(fill(rows[3]!).style.width).toBe('0%');
  });

  it('computes percentages of the total', () => {
    renderWithProviders(
      <DistributionBars
        ariaLabel="Workflows"
        items={[
          { key: 'a', label: 'A', text: 'A', value: 1 },
          { key: 'b', label: 'B', text: 'B', value: 3 },
        ]}
      />,
    );
    expect(screen.getByRole('listitem', { name: 'A: 1 (25%)' })).toBeInTheDocument();
    const b = screen.getByRole('listitem', { name: 'B: 3 (75%)' });
    expect(fill(b).style.width).toBe('100%');
    expect(within(b).getByText('· 75%', { exact: false })).toBeInTheDocument();
  });

  it('uses the per-item colour, else the list colour, else the brand colour', () => {
    const { unmount } = renderWithProviders(<DistributionBars items={items} ariaLabel="x" />);
    const rows = screen.getAllByRole('listitem');
    expect(fill(rows[0]!).style.backgroundColor).toBe('var(--mantine-color-nuvexa-6)');
    expect(fill(rows[1]!).style.backgroundColor).toBe('var(--mantine-color-red-5)');
    unmount();
    renderWithProviders(<DistributionBars items={items} ariaLabel="x" color="var(--mantine-color-teal-5)" />);
    expect(fill(screen.getAllByRole('listitem')[0]!).style.backgroundColor).toBe(
      'var(--mantine-color-teal-5)',
    );
  });

  it('shows a tooltip with the exact value on hover', async () => {
    const user = userEvent.setup();
    renderWithProviders(<DistributionBars items={items} ariaLabel="x" />);
    const track = screen.getAllByRole('listitem')[2]!.querySelector('.nuvexa-bar-track')!;
    await user.hover(track);
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Information only: 10,000 (100%)');
  });
});
