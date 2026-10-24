import { useMantineTheme } from '@mantine/core';
import { useDatesContext } from '@mantine/dates';
import { useQueryClient } from '@tanstack/react-query';
import { act, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AppProviders } from './App';
import { createQueryClient } from './lib/queryClient';
import { notifySuccess } from './lib/notify';

function Probe({ expected }: { expected: unknown }) {
  const client = useQueryClient();
  const theme = useMantineTheme();
  const dates = useDatesContext();
  return (
    <ul>
      <li>same client: {String(client === expected)}</li>
      <li>primary: {theme.primaryColor}</li>
      <li>first day of week: {dates.firstDayOfWeek}</li>
    </ul>
  );
}

describe('<AppProviders>', () => {
  it('renders children with the given QueryClient, the NUVEXA theme and Monday-first dates', () => {
    const client = createQueryClient();
    render(
      <AppProviders queryClient={client}>
        <Probe expected={client} />
      </AppProviders>,
    );
    expect(screen.getByText('same client: true')).toBeInTheDocument();
    expect(screen.getByText('primary: nuvexa')).toBeInTheDocument();
    expect(screen.getByText('first day of week: 1')).toBeInTheDocument();
  });

  it('mounts the notifications host, so toasts are rendered', async () => {
    render(
      <AppProviders queryClient={createQueryClient()}>
        <p>app</p>
      </AppProviders>,
    );
    act(() => notifySuccess('Settings saved', 'Tenant settings are now at version 3.'));
    expect(await screen.findByText('Settings saved')).toBeInTheDocument();
    expect(screen.getByText('Tenant settings are now at version 3.')).toBeInTheDocument();
  });
});
