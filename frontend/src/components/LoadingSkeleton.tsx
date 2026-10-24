import { Card, Group, Skeleton, Stack } from '@mantine/core';

export function TableSkeleton({ rows = 6, label = 'Loading' }: { rows?: number; label?: string }) {
  return (
    <Stack gap="sm" role="status" aria-busy="true" aria-label={label}>
      <Skeleton height={36} />
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} height={44} />
      ))}
    </Stack>
  );
}

export function CardsSkeleton({ count = 6, height = 96 }: { count?: number; height?: number }) {
  return (
    <Group grow gap="md" role="status" aria-busy="true" aria-label="Loading">
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} height={height} />
      ))}
    </Group>
  );
}

export function DetailSkeleton() {
  return (
    <Stack gap="md" role="status" aria-busy="true" aria-label="Loading">
      <Skeleton height={32} width="40%" />
      <Skeleton height={16} width="60%" />
      <Card>
        <Stack gap="sm">
          <Skeleton height={14} />
          <Skeleton height={14} />
          <Skeleton height={14} width="80%" />
          <Skeleton height={160} />
        </Stack>
      </Card>
    </Stack>
  );
}
