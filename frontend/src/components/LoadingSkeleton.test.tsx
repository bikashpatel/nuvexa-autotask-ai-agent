import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { CardsSkeleton, DetailSkeleton, TableSkeleton } from './LoadingSkeleton';

function skeletons(el: HTMLElement) {
  return el.querySelectorAll('.mantine-Skeleton-root');
}

describe('loading skeletons', () => {
  it('TableSkeleton is a busy, named status region with a header + 6 rows by default', async () => {
    renderWithProviders(<TableSkeleton />);
    const region = screen.getByRole('status', { name: 'Loading' });
    expect(region).toHaveAttribute('aria-busy', 'true');
    expect(skeletons(region)).toHaveLength(7);
    // Regression: aria-label on a role-less <div> is prohibited ARIA (axe "aria-prohibited-attr").
    await expectNoSeriousA11yViolations();
  });

  it('TableSkeleton accepts a row count and a custom label', () => {
    renderWithProviders(<TableSkeleton rows={2} label="Loading emails" />);
    const region = screen.getByRole('status', { name: 'Loading emails' });
    expect(skeletons(region)).toHaveLength(3);
  });

  it('CardsSkeleton renders the requested number of cards', async () => {
    const { unmount } = renderWithProviders(<CardsSkeleton />);
    expect(skeletons(screen.getByRole('status', { name: 'Loading' }))).toHaveLength(6);
    await expectNoSeriousA11yViolations();
    unmount();
    renderWithProviders(<CardsSkeleton count={3} height={40} />);
    const region = screen.getByRole('status', { name: 'Loading' });
    expect(region).toHaveAttribute('aria-busy', 'true');
    expect(skeletons(region)).toHaveLength(3);
  });

  it('DetailSkeleton is a busy, named status region', async () => {
    renderWithProviders(<DetailSkeleton />);
    const region = screen.getByRole('status', { name: 'Loading' });
    expect(region).toHaveAttribute('aria-busy', 'true');
    expect(skeletons(region)).toHaveLength(6);
    await expectNoSeriousA11yViolations();
  });
});
