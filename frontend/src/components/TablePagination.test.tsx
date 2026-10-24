import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { TablePagination } from './TablePagination';

function setup(props: { total: number; limit?: number; offset?: number }) {
  const onOffsetChange = vi.fn();
  renderWithProviders(
    <TablePagination
      total={props.total}
      limit={props.limit ?? 25}
      offset={props.offset ?? 0}
      onOffsetChange={onOffsetChange}
    />,
  );
  return { onOffsetChange };
}

describe('<TablePagination>', () => {
  it('shows the visible range in a polite live region', async () => {
    setup({ total: 60 });
    const range = screen.getByText('1–25 of 60');
    expect(range).toHaveAttribute('aria-live', 'polite');
    await expectNoSeriousA11yViolations();
  });

  it('shows the last partial page range', () => {
    setup({ total: 60, offset: 50 });
    expect(screen.getByText('51–60 of 60')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Page 3' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: 'next page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'previous page' })).toBeEnabled();
  });

  it('formats large numbers', () => {
    setup({ total: 12_345, limit: 50, offset: 1_000 });
    expect(screen.getByText('1,001–1,050 of 12,345')).toBeInTheDocument();
  });

  it('shows 0–0 of 0 and no page controls when empty', () => {
    setup({ total: 0 });
    expect(screen.getByText('0–0 of 0')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('hides page controls when everything fits on one page', () => {
    setup({ total: 25 });
    expect(screen.getByText('1–25 of 25')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders named page and previous/next controls', async () => {
    setup({ total: 60 });
    expect(screen.getByRole('button', { name: 'Page 1' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: 'Page 2' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Page 3' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Page 4' })).toBeNull();
    expect(screen.getByRole('button', { name: 'previous page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'next page' })).toBeEnabled();
    await expectNoSeriousA11yViolations();
  });

  it('reports the offset of the chosen page', async () => {
    const user = userEvent.setup();
    const { onOffsetChange } = setup({ total: 60 });
    await user.click(screen.getByRole('button', { name: 'Page 3' }));
    expect(onOffsetChange).toHaveBeenLastCalledWith(50);
    await user.click(screen.getByRole('button', { name: 'next page' }));
    expect(onOffsetChange).toHaveBeenLastCalledWith(25);
  });

  it('goes back to offset 0 from page 2', async () => {
    const user = userEvent.setup();
    const { onOffsetChange } = setup({ total: 60, offset: 25 });
    await user.click(screen.getByRole('button', { name: 'previous page' }));
    expect(onOffsetChange).toHaveBeenLastCalledWith(0);
  });

  it('is operable from the keyboard', async () => {
    const user = userEvent.setup();
    const { onOffsetChange } = setup({ total: 60 });
    screen.getByRole('button', { name: 'Page 2' }).focus();
    await user.keyboard('{Enter}');
    expect(onOffsetChange).toHaveBeenLastCalledWith(25);
    screen.getByRole('button', { name: 'Page 3' }).focus();
    await user.keyboard(' ');
    expect(onOffsetChange).toHaveBeenLastCalledWith(50);
  });

  it('clamps an out-of-range offset to the last page and shows an empty range (regression: "501–60 of 60")', () => {
    setup({ total: 60, offset: 500 });
    expect(screen.getByText('0–0 of 60')).toBeInTheDocument();
    expect(screen.queryByText(/501/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Page 3' })).toHaveAttribute('aria-current', 'page');
  });

  it('treats an offset exactly at the end as an empty page', () => {
    setup({ total: 50, offset: 50 });
    expect(screen.getByText('0–0 of 50')).toBeInTheDocument();
  });
});
