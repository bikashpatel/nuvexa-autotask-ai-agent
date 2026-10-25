import { ActionIcon, Badge, Group, Progress, Stack, Text, Tooltip } from '@mantine/core';
import { IconInfoCircle } from '@tabler/icons-react';

import type { ConfidenceBucket } from '../api/types';
import { confidenceTooltip } from '../lib/confidence';
import { lookup } from '../lib/lookup';

const BUCKET_COLOR: Record<ConfidenceBucket, string> = { high: 'teal', medium: 'yellow', low: 'red' };
const BUCKET_LABEL: Record<ConfidenceBucket, string> = {
  high: 'High confidence',
  medium: 'Medium confidence',
  low: 'Low confidence',
};

export function ConfidenceIndicator({
  score,
  bucket,
  method,
  compact = false,
}: {
  score: number;
  bucket: ConfidenceBucket;
  method?: string | null;
  compact?: boolean;
}) {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(score) ? score : 0));
  const color = lookup(BUCKET_COLOR, bucket) ?? 'gray';
  const label = lookup(BUCKET_LABEL, bucket) ?? bucket;
  const tooltip = confidenceTooltip(method);

  if (compact) {
    return (
      <Tooltip label={tooltip}>
        <Badge
          variant="light"
          color={color}
          className="nuvexa-tabular"
          aria-label={`${label}, heuristic score ${clamped.toFixed(2)}. ${tooltip}`}
        >
          {clamped.toFixed(2)} · {bucket}
        </Badge>
      </Tooltip>
    );
  }

  return (
    <Stack gap={6}>
      <Group gap="xs" justify="space-between" wrap="nowrap">
        <Group gap={6} wrap="nowrap">
          <Text fw={650} size="xl" lh={1}>
            {clamped.toFixed(2)}
          </Text>
          <Badge variant="light" color={color}>
            {label}
          </Badge>
        </Group>
        <Tooltip label={tooltip}>
          <ActionIcon variant="subtle" color="gray" size="sm" aria-label={`About this score: ${tooltip}`}>
            <IconInfoCircle size={16} aria-hidden />
          </ActionIcon>
        </Tooltip>
      </Group>
      <Progress
        value={clamped * 100}
        color={color}
        size="sm"
        radius="sm"
        aria-label={`Heuristic confidence ${clamped.toFixed(2)} (${label})`}
      />
      <Text size="xs" c="dimmed">
        Heuristic estimate · {method || 'heuristic-v1'}
      </Text>
    </Stack>
  );
}
