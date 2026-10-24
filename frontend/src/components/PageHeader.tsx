import { Group, Stack, Text, Title } from '@mantine/core';
import type { ReactNode } from 'react';

export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <Group justify="space-between" align="flex-start" gap="md" mb="lg" wrap="wrap">
      <Stack gap={4} style={{ minWidth: 0, flex: '1 1 320px' }}>
        {eyebrow ? (
          <Text size="xs" tt="uppercase" fw={600} c="dimmed" lts={0.6}>
            {eyebrow}
          </Text>
        ) : null}
        <Title order={1} style={{ overflowWrap: 'anywhere' }}>
          {title}
        </Title>
        {description ? (
          <Text c="dimmed" size="sm" maw={760}>
            {description}
          </Text>
        ) : null}
      </Stack>
      {actions ? (
        <Group gap="sm" wrap="wrap">
          {actions}
        </Group>
      ) : null}
    </Group>
  );
}
