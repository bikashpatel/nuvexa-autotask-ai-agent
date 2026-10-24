import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { EmptyPanel } from './EmptyPanel';

describe('<EmptyPanel>', () => {
  it('renders icon, title, description and an actionable call to action', async () => {
    const user = userEvent.setup();
    const onSync = vi.fn();
    renderWithProviders(
      <EmptyPanel
        icon={<svg data-testid="icon" aria-hidden />}
        title="No emails yet — sync your mailbox"
        description="Pull the latest messages from your connected mailbox."
        action={
          <button type="button" onClick={onSync}>
            Sync mailbox
          </button>
        }
      />,
    );
    expect(screen.getByTestId('icon')).toBeInTheDocument();
    expect(screen.getByText('No emails yet — sync your mailbox')).toBeInTheDocument();
    expect(screen.getByText('Pull the latest messages from your connected mailbox.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Sync mailbox' }));
    expect(onSync).toHaveBeenCalledTimes(1);
    await expectNoSeriousA11yViolations();
  });

  it('omits the description and actions when not given', () => {
    const { container } = renderWithProviders(<EmptyPanel icon={null} title="Nothing here" />);
    expect(screen.getByText('Nothing here')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
    expect(container.querySelector('.mantine-EmptyState-actions')).toBeNull();
    expect(container.querySelector('.mantine-EmptyState-description')).toBeNull();
  });
});
