import { Alert, Badge, Code, Group, List, Stack, Text } from '@mantine/core';
import { IconShieldExclamation } from '@tabler/icons-react';

import type { InjectionFlag } from '../api/types';
import { humanize } from '../lib/format';
import { lookup } from '../lib/lookup';

const SEVERITY_COLOR: Record<InjectionFlag['severity'], string> = {
  high: 'red',
  medium: 'orange',
  low: 'yellow',
};

/**
 * Prominent warning shown whenever the deterministic screener flagged prompt-injection
 * indicators in the email. Excerpts are untrusted and rendered as plain text.
 */
export function InjectionWarning({ flags }: { flags: InjectionFlag[] | null | undefined }) {
  if (!flags || flags.length === 0) return null;
  const highest = flags.some((f) => f.severity === 'high')
    ? 'high'
    : flags.some((f) => f.severity === 'medium')
      ? 'medium'
      : 'low';
  return (
    <Alert
      role="alert"
      aria-live="polite"
      color="red"
      variant="filled"
      radius="md"
      icon={<IconShieldExclamation size={24} aria-hidden />}
      title={`Possible prompt injection detected (${flags.length} indicator${flags.length === 1 ? '' : 's'}, highest severity: ${highest})`}
      data-testid="injection-warning"
    >
      <Stack gap="sm">
        <Text size="sm">
          This email contains text that looks like instructions aimed at the AI. NUVEXA treated it strictly as
          data and lowered the confidence score — the model cannot approve or execute anything. Review every
          proposed field against the original email before approving.
        </Text>
        <List spacing="xs" size="sm" listStyleType="none" p={0}>
          {flags.map((flag, index) => (
            <List.Item key={`${flag.rule}-${index}`}>
              <Group gap="xs" wrap="wrap" align="flex-start">
                <Badge color={lookup(SEVERITY_COLOR, flag.severity) ?? 'gray'} variant="white" size="sm">
                  {flag.severity}
                </Badge>
                <Text size="sm" fw={600}>
                  {humanize(flag.rule)}
                </Text>
                {flag.excerpt ? (
                  <Code
                    fz="xs"
                    style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxWidth: '100%' }}
                    aria-label="Flagged excerpt"
                  >
                    {flag.excerpt}
                  </Code>
                ) : null}
              </Group>
            </List.Item>
          ))}
        </List>
      </Stack>
    </Alert>
  );
}
