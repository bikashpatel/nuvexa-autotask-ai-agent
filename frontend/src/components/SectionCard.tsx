import { Card, Group, Text, Title } from '@mantine/core';
import type { ReactNode } from 'react';

export function SectionCard({
  title,
  description,
  actions,
  children,
  icon,
  id,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  icon?: ReactNode;
  id?: string;
}) {
  const headingId = id ? `${id}-heading` : undefined;
  return (
    <Card component="section" aria-labelledby={headingId} id={id}>
      <Group justify="space-between" align="flex-start" mb="md" gap="sm" wrap="wrap">
        <div style={{ minWidth: 0 }}>
          <Group gap="xs" wrap="nowrap">
            {icon}
            <Title order={2} size="h3" id={headingId}>
              {title}
            </Title>
          </Group>
          {description ? (
            <Text size="sm" c="dimmed" mt={4}>
              {description}
            </Text>
          ) : null}
        </div>
        {actions ? <Group gap="xs">{actions}</Group> : null}
      </Group>
      {children}
    </Card>
  );
}
