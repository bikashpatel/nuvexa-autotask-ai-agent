import { Anchor, Code, Group, Text, VisuallyHidden } from '@mantine/core';
import { IconExternalLink } from '@tabler/icons-react';
import type { ReactNode } from 'react';

import { classifyUrl } from '../lib/urls';
import { MockBadge } from './DomainBadges';

/**
 * Renders an external URL safely:
 *  - http(s) → new tab with rel="noopener noreferrer"
 *  - mock:// → plain text with a "Mock" badge (never a link)
 *  - anything else (javascript:, data:, …) → plain text, never a link
 */
export function ExternalLink({ url, children }: { url: string | null | undefined; children?: ReactNode }) {
  if (!url)
    return (
      <Text span c="dimmed">
        —
      </Text>
    );
  const kind = classifyUrl(url);
  if (kind === 'http') {
    return (
      <Anchor
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        size="sm"
        style={{ wordBreak: 'break-all' }}
      >
        <Group gap={4} wrap="nowrap" component="span" style={{ display: 'inline-flex' }}>
          <span>{children ?? url}</span>
          <IconExternalLink size={14} aria-hidden />
          <VisuallyHidden>(opens in a new tab)</VisuallyHidden>
        </Group>
      </Anchor>
    );
  }
  return (
    <Group gap={6} wrap="nowrap" component="span" style={{ display: 'inline-flex', maxWidth: '100%' }}>
      <Code style={{ wordBreak: 'break-all' }}>{url}</Code>
      {kind === 'mock' ? <MockBadge /> : null}
    </Group>
  );
}
