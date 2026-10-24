import { Alert, Button, Code, Group, Stack, Text } from '@mantine/core';
import { IconAlertTriangle, IconLock, IconRefresh, IconSearchOff, IconWifiOff } from '@tabler/icons-react';

import { isApiError } from '../api/client';
import { errorMessage } from '../lib/notify';

export interface ErrorStateProps {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  retrying?: boolean;
  compact?: boolean;
}

/** Error panel with retry and the support reference (correlation id / X-Request-ID). */
export function ErrorState({ error, title, onRetry, retrying, compact }: ErrorStateProps) {
  const apiError = isApiError(error) ? error : null;
  let heading = title ?? 'Something went wrong';
  let Icon = IconAlertTriangle;
  let color = 'red';
  if (apiError?.isNotFound) {
    heading = title ?? 'Not found';
    Icon = IconSearchOff;
    color = 'gray';
  } else if (apiError?.isForbidden) {
    heading = title ?? 'You do not have access to this';
    Icon = IconLock;
    color = 'orange';
  } else if (apiError?.isNetworkError) {
    heading = title ?? 'API unreachable';
    Icon = IconWifiOff;
  }

  const message = apiError?.isNotFound
    ? 'It may have been removed, or it is not visible to your account.'
    : errorMessage(error);

  return (
    <Alert
      color={color}
      variant="light"
      title={heading}
      icon={<Icon size={20} aria-hidden />}
      role="alert"
      p={compact ? 'sm' : 'md'}
    >
      <Stack gap="xs">
        <Text size="sm">{message}</Text>
        {apiError ? (
          <Group gap="xs">
            <Text size="xs" c="dimmed">
              Support reference
            </Text>
            <Code fz="xs">{apiError.supportReference}</Code>
            <Text size="xs" c="dimmed">
              ({apiError.code}, HTTP {apiError.status || 'n/a'})
            </Text>
          </Group>
        ) : null}
        {onRetry ? (
          <Group>
            <Button
              size="xs"
              variant="light"
              color={color}
              leftSection={<IconRefresh size={14} aria-hidden />}
              onClick={onRetry}
              loading={retrying}
            >
              Retry
            </Button>
          </Group>
        ) : null}
      </Stack>
    </Alert>
  );
}
