import { Badge, type BadgeProps } from '@mantine/core';
import {
  IconAlertOctagon,
  IconArrowDown,
  IconArrowUp,
  IconBan,
  IconBug,
  IconBulb,
  IconCategory,
  IconChecklist,
  IconChevronsUp,
  IconFlame,
  IconInfoCircle,
  IconLifebuoy,
  IconMinus,
  IconMessageForward,
} from '@tabler/icons-react';

import type { EmailCategory, Priority, Urgency } from '../api/types';
import { categoryLabel, priorityLabel, URGENCY_LABELS } from '../lib/format';

const CATEGORY_ICONS: Record<EmailCategory, typeof IconBug> = {
  incident: IconFlame,
  bug: IconBug,
  service_request: IconLifebuoy,
  task_request: IconChecklist,
  feature_request: IconBulb,
  follow_up: IconMessageForward,
  information_only: IconInfoCircle,
  spam_or_irrelevant: IconBan,
  other: IconCategory,
};

export function CategoryBadge({ category, ...rest }: { category: string | null | undefined } & BadgeProps) {
  if (!category) {
    return (
      <Badge variant="outline" color="gray" {...rest}>
        Unclassified
      </Badge>
    );
  }
  const Icon = CATEGORY_ICONS[category as EmailCategory] ?? IconCategory;
  return (
    <Badge variant="default" leftSection={<Icon size={12} aria-hidden />} {...rest}>
      {categoryLabel(category)}
    </Badge>
  );
}

const PRIORITY_STYLE: Record<Priority, { color: string; icon: typeof IconBug }> = {
  critical: { color: 'red', icon: IconAlertOctagon },
  high: { color: 'orange', icon: IconChevronsUp },
  medium: { color: 'yellow', icon: IconArrowUp },
  low: { color: 'gray', icon: IconArrowDown },
};

export function PriorityBadge({ priority, ...rest }: { priority: string | null | undefined } & BadgeProps) {
  if (!priority) return null;
  const style = PRIORITY_STYLE[priority as Priority] ?? { color: 'gray', icon: IconMinus };
  const Icon = style.icon;
  return (
    <Badge
      variant="light"
      color={style.color}
      leftSection={<Icon size={12} aria-hidden />}
      aria-label={`Priority: ${priorityLabel(priority)}`}
      {...rest}
    >
      {priorityLabel(priority)}
    </Badge>
  );
}

export function UrgencyBadge({ urgency, ...rest }: { urgency: Urgency | null | undefined } & BadgeProps) {
  if (!urgency) return null;
  const color = urgency === 'immediate' ? 'red' : urgency === 'high' ? 'orange' : 'gray';
  return (
    <Badge variant="outline" color={color} aria-label={`Urgency: ${URGENCY_LABELS[urgency]}`} {...rest}>
      Urgency: {URGENCY_LABELS[urgency] ?? urgency}
    </Badge>
  );
}

export function MockBadge(props: BadgeProps) {
  return (
    <Badge variant="filled" color="grape" size="xs" {...props}>
      Mock
    </Badge>
  );
}
