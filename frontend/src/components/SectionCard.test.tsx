import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { SectionCard } from './SectionCard';

describe('<SectionCard>', () => {
  it('is a region labelled by its h2 when an id is given', async () => {
    renderWithProviders(
      <SectionCard
        id="analysis"
        title="AI analysis"
        description="Attempt #1 · heuristic"
        icon={<svg data-testid="icon" aria-hidden />}
        actions={<button type="button">Re-check</button>}
      >
        <p>Body content</p>
      </SectionCard>,
    );
    const region = screen.getByRole('region', { name: 'AI analysis' });
    expect(region).toHaveAttribute('id', 'analysis');
    expect(region.tagName).toBe('SECTION');
    const heading = within(region).getByRole('heading', { level: 2, name: 'AI analysis' });
    expect(heading).toHaveAttribute('id', 'analysis-heading');
    expect(region).toHaveAttribute('aria-labelledby', 'analysis-heading');
    expect(within(region).getByText('Attempt #1 · heuristic')).toBeInTheDocument();
    expect(within(region).getByTestId('icon')).toBeInTheDocument();
    expect(within(region).getByRole('button', { name: 'Re-check' })).toBeInTheDocument();
    expect(within(region).getByText('Body content')).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
  });

  it('has no aria-labelledby (and so no region role) without an id', () => {
    const { container } = renderWithProviders(
      <SectionCard title="Run">
        <span>facts</span>
      </SectionCard>,
    );
    const section = container.querySelector('section')!;
    expect(section).not.toHaveAttribute('aria-labelledby');
    expect(screen.queryByRole('region')).toBeNull();
    expect(screen.getByRole('heading', { level: 2, name: 'Run' })).not.toHaveAttribute('id');
    expect(screen.queryByRole('button')).toBeNull();
    // No description paragraph.
    expect(section.querySelectorAll('p')).toHaveLength(0);
  });
});
