import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { ConfirmModal } from './ConfirmModal';

function setup(props: Partial<Parameters<typeof ConfirmModal>[0]> = {}) {
  const onClose = vi.fn();
  const onConfirm = vi.fn();
  const result = renderWithProviders(
    <ConfirmModal
      opened
      onClose={onClose}
      onConfirm={onConfirm}
      title="Disconnect this mailbox?"
      confirmLabel="Disconnect"
      {...props}
    >
      {props.children ?? 'Stored tokens are deleted.'}
    </ConfirmModal>,
  );
  return { ...result, onClose, onConfirm };
}

describe('<ConfirmModal>', () => {
  it('renders nothing while closed', () => {
    setup({ opened: false });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('is a modal dialog named by its title with string children as text', async () => {
    setup();
    const dialog = screen.getByRole('dialog', { name: 'Disconnect this mailbox?' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByText('Stored tokens are deleted.').tagName).toBe('P');
    await expectNoSeriousA11yViolations();
  });

  it('renders node children as given', () => {
    setup({
      children: (
        <ul aria-label="Consequences">
          <li>Sync stops</li>
        </ul>
      ),
    });
    expect(screen.getByRole('list', { name: 'Consequences' })).toBeInTheDocument();
    expect(screen.getByText('Sync stops')).toBeInTheDocument();
  });

  it('calls onConfirm / onClose from the buttons', async () => {
    const user = userEvent.setup();
    const { onClose, onConfirm } = setup();
    await user.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('has a named close button (regression: the X button had no accessible name)', async () => {
    const user = userEvent.setup();
    const { onClose } = setup();
    await user.click(screen.getByRole('button', { name: 'Close dialog' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on Escape while idle', async () => {
    const user = userEvent.setup();
    const { onClose } = setup();
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('cannot be dismissed while the confirmation is in flight', async () => {
    const user = userEvent.setup();
    const { onClose, onConfirm } = setup({ loading: true });
    const confirm = screen.getByRole('button', { name: 'Disconnect' });
    expect(confirm).toHaveAttribute('data-loading');
    expect(confirm).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Close dialog' })).toBeDisabled();
    await user.keyboard('{Escape}');
    await user.click(confirm);
    expect(onClose).not.toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('moves focus into the dialog and traps Tab / Shift+Tab inside it', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <>
        <button type="button">Outside before</button>
        <ConfirmModal
          opened
          onClose={() => undefined}
          onConfirm={() => undefined}
          title="T"
          confirmLabel="OK"
        >
          Body
        </ConfirmModal>
        <button type="button">Outside after</button>
      </>,
    );
    const dialog = screen.getByRole('dialog', { name: 'T' });
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement));
    const visited = new Set<string>();
    for (let i = 0; i < 6; i += 1) {
      await user.tab();
      expect(dialog).toContainElement(document.activeElement as HTMLElement);
      visited.add(
        document.activeElement?.textContent || document.activeElement?.getAttribute('aria-label') || '',
      );
    }
    expect(visited).toEqual(new Set(['Cancel', 'OK', 'Close dialog']));
    for (let i = 0; i < 4; i += 1) {
      await user.tab({ shift: true });
      expect(dialog).toContainElement(document.activeElement as HTMLElement);
    }
  });

  it('activates the confirm button from the keyboard', async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup();
    // Let the focus trap place its initial focus first, then move to the confirm button.
    await waitFor(() =>
      expect(screen.getByRole('dialog')).toContainElement(document.activeElement as HTMLElement),
    );
    screen.getByRole('button', { name: 'Disconnect' }).focus();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toHaveFocus();
    await user.keyboard('{Enter}');
    await user.keyboard(' ');
    expect(onConfirm).toHaveBeenCalledTimes(2);
  });

  it('uses the confirm colour (default brand, red for destructive actions)', () => {
    const { unmount } = setup();
    expect(screen.getByRole('button', { name: 'Disconnect' }).getAttribute('style')).toContain(
      '--mantine-color-nuvexa-filled',
    );
    unmount();
    setup({ confirmColor: 'red' });
    expect(screen.getByRole('button', { name: 'Disconnect' }).getAttribute('style')).toContain(
      '--mantine-color-red-filled',
    );
  });
});
