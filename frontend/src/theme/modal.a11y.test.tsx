import { MantineProvider, Modal } from '@mantine/core';
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { theme } from './theme';

describe('Modal theme default', () => {
  it('gives every Mantine Modal close (×) button an accessible name (regression: axe button-name)', async () => {
    render(
      <MantineProvider theme={theme} env="test">
        <Modal opened onClose={() => undefined} title="Any dialog">
          Body
        </Modal>
      </MantineProvider>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Any dialog' });
    expect(within(dialog).getByRole('button', { name: 'Close dialog' })).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
  });

  it('lets a modal override the default label', () => {
    render(
      <MantineProvider theme={theme} env="test">
        <Modal opened onClose={() => undefined} title="Custom" closeButtonProps={{ 'aria-label': 'Dismiss' }}>
          Body
        </Modal>
      </MantineProvider>,
    );
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument();
  });
});
