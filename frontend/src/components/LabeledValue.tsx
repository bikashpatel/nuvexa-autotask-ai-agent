import { DataList } from '@mantine/core';
import type { ReactNode } from 'react';

export interface LabeledItem {
  label: string;
  value: ReactNode;
  hidden?: boolean;
}

/** Definition list (dl/dt/dd) for key-value facts — accessible and compact. */
export function FactList({
  items,
  labelWidth = 150,
  orientation = 'horizontal',
}: {
  items: LabeledItem[];
  labelWidth?: number | string;
  orientation?: 'horizontal' | 'vertical';
}) {
  return (
    <DataList size="sm" gap="xs" labelWidth={labelWidth} orientation={orientation}>
      {items
        .filter((item) => !item.hidden)
        .map((item) => (
          <DataList.Item key={item.label}>
            <DataList.ItemLabel c="dimmed">{item.label}</DataList.ItemLabel>
            <DataList.ItemValue style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
              {item.value}
            </DataList.ItemValue>
          </DataList.Item>
        ))}
    </DataList>
  );
}
