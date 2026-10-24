import { Badge, Tooltip, type BadgeProps } from '@mantine/core';

import { getStatusDescriptor, type StatusKind } from '../theme/status';
import { StatusIcon } from './StatusIcon';

export interface StatusBadgeProps extends Omit<BadgeProps, 'color' | 'variant' | 'children'> {
  kind: StatusKind;
  status: string;
  /** Show the descriptor's explanation in a tooltip. */
  withTooltip?: boolean;
}

export function StatusBadge({ kind, status, withTooltip = false, size = 'md', ...rest }: StatusBadgeProps) {
  const descriptor = getStatusDescriptor(kind, status);
  const badge = (
    <Badge
      size={size}
      color={descriptor.color}
      variant={descriptor.variant}
      leftSection={
        <StatusIcon name={descriptor.icon} loading={descriptor.loading} color={descriptor.color} />
      }
      opacity={descriptor.dimmed ? 0.6 : undefined}
      data-status={status}
      data-tone={descriptor.tone}
      aria-label={`Status: ${descriptor.label}`}
      {...rest}
    >
      {descriptor.label}
    </Badge>
  );
  if (withTooltip && descriptor.description) {
    return <Tooltip label={descriptor.description}>{badge}</Tooltip>;
  }
  return badge;
}
