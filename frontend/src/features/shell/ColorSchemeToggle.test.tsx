import { MantineProvider, localStorageColorSchemeManager, type MantineColorScheme } from '@mantine/core';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../../test/axe';
import { theme } from '../../theme/theme';
import { ColorSchemeToggle } from './ColorSchemeToggle';

const STORAGE_KEY = 'nuvexa-test-color-scheme';

function renderToggle(defaultColorScheme: MantineColorScheme = 'light') {
  return render(
    <MantineProvider
      theme={theme}
      env="test"
      defaultColorScheme={defaultColorScheme}
      colorSchemeManager={localStorageColorSchemeManager({ key: STORAGE_KEY })}
    >
      <ColorSchemeToggle />
    </MantineProvider>,
  );
}

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute('data-mantine-color-scheme');
});

describe('<ColorSchemeToggle>', () => {
  it('in light mode offers the dark theme (moon icon), then flips after a click', async () => {
    const user = userEvent.setup();
    const { container } = renderToggle('light');
    const button = await screen.findByRole('button', { name: 'Switch to dark theme' });
    expect(container.querySelector('.tabler-icon-moon')).not.toBeNull();
    expect(container.querySelector('.tabler-icon-sun')).toBeNull();

    await user.click(button);
    expect(await screen.findByRole('button', { name: 'Switch to light theme' })).toBeInTheDocument();
    expect(container.querySelector('.tabler-icon-sun')).not.toBeNull();
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('dark');
    expect(document.documentElement).toHaveAttribute('data-mantine-color-scheme', 'dark');
  });

  it('in dark mode offers the light theme and toggles back from the keyboard', async () => {
    const user = userEvent.setup();
    renderToggle('dark');
    const button = await screen.findByRole('button', { name: 'Switch to light theme' });
    await user.tab();
    expect(button).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(await screen.findByRole('button', { name: 'Switch to dark theme' })).toBeInTheDocument();
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('light');
    await user.keyboard(' ');
    expect(await screen.findByRole('button', { name: 'Switch to light theme' })).toBeInTheDocument();
  });

  it('restores a stored preference', async () => {
    window.localStorage.setItem(STORAGE_KEY, 'dark');
    renderToggle('light');
    expect(await screen.findByRole('button', { name: 'Switch to light theme' })).toBeInTheDocument();
  });

  it('has no serious accessibility violations', async () => {
    renderToggle('light');
    await screen.findByRole('button', { name: 'Switch to dark theme' });
    await expectNoSeriousA11yViolations();
  });
});
