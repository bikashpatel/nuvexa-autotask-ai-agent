import { Box, Group, Stack, Text, Tooltip } from '@mantine/core';
import type { ReactNode } from 'react';

import { formatInteger } from '../lib/format';

export interface DistributionItem {
  key: string;
  label: ReactNode;
  /** Plain-text label for screen readers and tooltips. */
  text: string;
  value: number;
  color?: string;
}

/**
 * Lightweight horizontal bar list (no chart library). Thin bars from a common baseline, value in
 * text ink at the tip, a per-bar tooltip, and an accessible list structure (label + value are
 * always present as text, so colour is never the only signal).
 */
export function DistributionBars({
  items,
  color = 'var(--mantine-color-nuvexa-6)',
  emptyText = 'No data yet.',
  ariaLabel,
}: {
  items: DistributionItem[];
  color?: string;
  emptyText?: string;
  ariaLabel: string;
}) {
  const total = items.reduce((sum, item) => sum + item.value, 0);
  const max = Math.max(1, ...items.map((item) => item.value));
  if (items.length === 0 || total === 0) {
    return (
      <Text size="sm" c="dimmed">
        {emptyText}
      </Text>
    );
  }
  return (
    <Stack component="ul" gap="sm" p={0} m={0} style={{ listStyle: 'none' }} aria-label={ariaLabel}>
      {items.map((item) => {
        const pct = total ? Math.round((item.value / total) * 100) : 0;
        const width = `${Math.max(item.value > 0 ? 2 : 0, (item.value / max) * 100)}%`;
        return (
          <Box component="li" key={item.key} aria-label={`${item.text}: ${item.value} (${pct}%)`}>
            <Group justify="space-between" gap="xs" mb={4} wrap="nowrap">
              <Box style={{ minWidth: 0 }}>{item.label}</Box>
              <Text size="sm" fw={600} className="nuvexa-tabular" aria-hidden>
                {formatInteger(item.value)}
                <Text span size="xs" c="dimmed" fw={400}>
                  {' '}
                  · {pct}%
                </Text>
              </Text>
            </Group>
            <Tooltip label={`${item.text}: ${formatInteger(item.value)} (${pct}%)`} position="top-start">
              <div className="nuvexa-bar-track" aria-hidden>
                <div className="nuvexa-bar-fill" style={{ width, backgroundColor: item.color ?? color }} />
              </div>
            </Tooltip>
          </Box>
        );
      })}
    </Stack>
  );
}
