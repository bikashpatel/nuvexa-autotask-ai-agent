import { Group, Pagination, Text } from '@mantine/core';

import { formatInteger } from '../lib/format';

export function TablePagination({
  total,
  limit,
  offset,
  onOffsetChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / limit));
  const page = Math.min(pages, Math.floor(offset / limit) + 1);
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(total, offset + limit);
  return (
    <Group justify="space-between" mt="md" gap="sm" wrap="wrap">
      <Text size="sm" c="dimmed" className="nuvexa-tabular" aria-live="polite">
        {formatInteger(from)}–{formatInteger(to)} of {formatInteger(total)}
      </Text>
      {pages > 1 ? (
        <Pagination
          total={pages}
          value={page}
          onChange={(next) => onOffsetChange((next - 1) * limit)}
          size="sm"
          getControlProps={(control) => ({ 'aria-label': `${control} page` })}
          getItemProps={(p) => ({ 'aria-label': `Page ${p}` })}
        />
      ) : null}
    </Group>
  );
}
