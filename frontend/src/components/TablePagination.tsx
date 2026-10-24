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
  // An offset at/after the end (stale or hand-edited URL) is an empty page, not "501–60 of 60".
  const empty = total === 0 || offset >= total;
  const from = empty ? 0 : offset + 1;
  const to = empty ? 0 : Math.min(total, offset + limit);
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
