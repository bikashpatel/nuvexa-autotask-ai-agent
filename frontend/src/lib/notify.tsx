import { Stack, Text } from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconAlertTriangle, IconCheck, IconInfoCircle } from '@tabler/icons-react';

import { isApiError } from '../api/client';

export function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message;
  if (error instanceof Error) return error.message;
  return 'Something went wrong.';
}

/** Error toast that always carries the support reference (correlation id or X-Request-ID). */
export function notifyError(title: string, error: unknown): void {
  const reference = isApiError(error) ? error.supportReference : null;
  const code = isApiError(error) ? error.code : null;
  notifications.show({
    color: 'red',
    title,
    icon: <IconAlertTriangle size={18} aria-hidden />,
    autoClose: 10_000,
    message: (
      <Stack gap={2}>
        <Text size="sm">{errorMessage(error)}</Text>
        {reference ? (
          <Text size="xs" c="dimmed" ff="monospace">
            {code ? `${code} · ` : ''}ref {reference}
          </Text>
        ) : null}
      </Stack>
    ),
  });
}

export function notifySuccess(title: string, message?: string): void {
  notifications.show({
    color: 'green',
    title,
    message: message ?? '',
    icon: <IconCheck size={18} aria-hidden />,
  });
}

export function notifyInfo(title: string, message?: string): void {
  notifications.show({
    color: 'blue',
    title,
    message: message ?? '',
    icon: <IconInfoCircle size={18} aria-hidden />,
  });
}
