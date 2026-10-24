import { Text, Tooltip, type TextProps } from '@mantine/core';

import { formatDateTime, formatIsoTitle, formatRelative } from '../lib/format';

/** Relative time with the absolute timestamp in a tooltip (and in the <time> element). */
export function TimeText({
  value,
  mode = 'relative',
  ...rest
}: { value: string | null | undefined; mode?: 'relative' | 'absolute' } & TextProps) {
  if (!value) {
    return (
      <Text span c="dimmed" {...rest}>
        —
      </Text>
    );
  }
  const text = mode === 'relative' ? formatRelative(value) : formatDateTime(value);
  return (
    <Tooltip label={`${formatDateTime(value)} · ${formatIsoTitle(value) ?? ''}`}>
      <Text span component="time" dateTime={value} {...rest}>
        {text}
      </Text>
    </Tooltip>
  );
}
