import { Card, Group, Stack, Text, ThemeIcon } from '@mantine/core';
import type { ReactNode } from 'react';
import { Link } from 'react-router';

export interface StatTileProps {
  label: string;
  value: string;
  hint?: ReactNode;
  icon?: ReactNode;
  accent?: string;
  to?: string;
}

/** Stat tile: sentence-case label · semibold compact value · optional context line. */
export function StatTile({ label, value, hint, icon, accent = 'nuvexa', to }: StatTileProps) {
  const body = (
    <Card padding="md" h="100%" style={to ? { transition: 'border-color 120ms ease' } : undefined}>
      <Stack gap={6}>
        <Group justify="space-between" wrap="nowrap" gap="xs">
          <Text size="sm" c="dimmed" fw={500}>
            {label}
          </Text>
          {icon ? (
            <ThemeIcon variant="light" color={accent} size={30} radius="md" aria-hidden>
              {icon}
            </ThemeIcon>
          ) : null}
        </Group>
        <Text fz={28} fw={650} lh={1.1}>
          {value}
        </Text>
        {hint ? (
          <Text size="xs" c="dimmed">
            {hint}
          </Text>
        ) : null}
      </Stack>
    </Card>
  );
  if (to) {
    return (
      <Link
        to={to}
        className="nuvexa-row-link"
        aria-label={`${label}: ${value}. Open`}
        style={{
          textDecoration: 'none',
          display: 'block',
          height: '100%',
          borderRadius: 'var(--mantine-radius-md)',
        }}
      >
        {body}
      </Link>
    );
  }
  return body;
}
