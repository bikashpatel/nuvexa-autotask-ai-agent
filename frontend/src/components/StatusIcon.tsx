import { Loader } from '@mantine/core';
import {
  IconAlertTriangle,
  IconBan,
  IconCheck,
  IconCircle,
  IconCircleCheck,
  IconClock,
  IconHelpHexagon,
  IconInbox,
  IconLayersOff,
  IconPlug,
  IconPlugOff,
  IconX,
} from '@tabler/icons-react';

import type { StatusIconName } from '../theme/status';

const ICONS = {
  clock: IconClock,
  check: IconCheck,
  loader: IconClock,
  'circle-check': IconCircleCheck,
  alert: IconAlertTriangle,
  help: IconHelpHexagon,
  x: IconX,
  ban: IconBan,
  'layers-off': IconLayersOff,
  circle: IconCircle,
  inbox: IconInbox,
  plug: IconPlug,
  'plug-off': IconPlugOff,
} satisfies Record<StatusIconName, unknown>;

export function StatusIcon({
  name,
  loading,
  size = 12,
  color,
}: {
  name: StatusIconName;
  loading?: boolean;
  size?: number;
  color?: string;
}) {
  if (loading) return <Loader size={size} color={color} type="oval" aria-hidden />;
  const Icon = ICONS[name];
  return <Icon size={size} stroke={2} aria-hidden />;
}
