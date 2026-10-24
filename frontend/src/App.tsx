import { MantineProvider } from '@mantine/core';
import { DatesProvider } from '@mantine/dates';
import { Notifications } from '@mantine/notifications';
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import { theme } from './theme/theme';

export function AppProviders({ queryClient, children }: { queryClient: QueryClient; children: ReactNode }) {
  return (
    <MantineProvider theme={theme} defaultColorScheme="auto">
      <DatesProvider settings={{ firstDayOfWeek: 1 }}>
        <Notifications position="top-right" limit={4} />
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </DatesProvider>
    </MantineProvider>
  );
}
