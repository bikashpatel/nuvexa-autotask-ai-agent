import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { PageHeader } from './PageHeader';

describe('<PageHeader>', () => {
  it('renders the title as the page h1 with eyebrow, description and actions', async () => {
    renderWithProviders(
      <PageHeader
        eyebrow="Contoso · operations overview"
        title="Welcome back, Avery"
        description="Email-to-work pipeline health."
        actions={<button type="button">Review approvals</button>}
      />,
    );
    expect(screen.getByRole('heading', { level: 1, name: 'Welcome back, Avery' })).toBeInTheDocument();
    expect(screen.getByText('Contoso · operations overview')).toBeInTheDocument();
    expect(screen.getByText('Email-to-work pipeline health.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Review approvals' })).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
  });

  it('renders only the title when nothing else is given', () => {
    const { container } = renderWithProviders(<PageHeader title="Inbox" />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Inbox');
    expect(screen.queryByRole('button')).toBeNull();
    // Title only: no eyebrow/description paragraphs.
    expect(container.querySelectorAll('p')).toHaveLength(0);
  });

  it('accepts a node as the title', () => {
    renderWithProviders(
      <PageHeader
        title={
          <>
            Approvals <small>beta</small>
          </>
        }
      />,
    );
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Approvals beta');
  });
});
