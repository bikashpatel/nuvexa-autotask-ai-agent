import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { StatTile } from './StatTile';

const target = [{ path: '/approvals', element: <h1>Approvals page</h1> }];

describe('<StatTile>', () => {
  it('renders label, value, hint and a decorative icon without a link', async () => {
    const { container } = renderWithProviders(
      <StatTile
        label="Completed"
        value="15"
        hint="Executed after approval"
        icon={<svg data-testid="icon" />}
        accent="green"
      />,
    );
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('15')).toBeInTheDocument();
    expect(screen.getByText('Executed after approval')).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
    const icon = screen.getByTestId('icon').closest('.mantine-ThemeIcon-root')!;
    expect(icon).toHaveAttribute('aria-hidden', 'true');
    expect(icon.getAttribute('style')).toContain('--mantine-color-green-light');
    expect(container.querySelectorAll('p')).toHaveLength(3);
    await expectNoSeriousA11yViolations();
  });

  it('omits hint and icon when not given', () => {
    const { container } = renderWithProviders(<StatTile label="Rejected" value="3" />);
    expect(container.querySelector('.mantine-ThemeIcon-root')).toBeNull();
    expect(container.querySelectorAll('p')).toHaveLength(2);
  });

  it('is a named link that navigates to its target on click', async () => {
    const user = userEvent.setup();
    const { router } = renderWithProviders(<StatTile label="Pending approvals" value="4" to="/approvals" />, {
      extraRoutes: target,
    });
    const link = screen.getByRole('link', { name: 'Pending approvals: 4. Open' });
    expect(link).toHaveAttribute('href', '/approvals');
    await expectNoSeriousA11yViolations();
    await user.click(link);
    expect(router.state.location.pathname).toBe('/approvals');
    expect(screen.getByRole('heading', { name: 'Approvals page' })).toBeInTheDocument();
  });

  it('is reachable and activatable from the keyboard', async () => {
    const user = userEvent.setup();
    const { router } = renderWithProviders(
      <StatTile label="Failed / uncertain" value="2" to="/approvals?view=attention" />,
      { extraRoutes: target },
    );
    await user.tab();
    expect(screen.getByRole('link', { name: 'Failed / uncertain: 2. Open' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(router.state.location.pathname).toBe('/approvals');
    expect(router.state.location.search).toBe('?view=attention');
  });
});
