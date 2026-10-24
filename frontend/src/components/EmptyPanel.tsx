import { EmptyState } from '@mantine/core';
import type { ReactNode } from 'react';

export function EmptyPanel({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <EmptyState py="xl" icon={icon} title={title} description={description} variant="light" color="nuvexa">
      {action ? <EmptyState.Actions>{action}</EmptyState.Actions> : null}
    </EmptyState>
  );
}
