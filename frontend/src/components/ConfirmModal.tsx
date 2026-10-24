import { Button, Group, Modal, Stack, Text } from '@mantine/core';
import type { ReactNode } from 'react';

export function ConfirmModal({
  opened,
  onClose,
  title,
  children,
  confirmLabel,
  confirmColor = 'nuvexa',
  onConfirm,
  loading,
}: {
  opened: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  confirmColor?: string;
  onConfirm: () => void;
  loading?: boolean;
}) {
  return (
    <Modal opened={opened} onClose={loading ? () => undefined : onClose} title={title}>
      <Stack gap="md">
        {typeof children === 'string' ? <Text size="sm">{children}</Text> : children}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose} disabled={loading}>
            Cancel
          </Button>
          <Button color={confirmColor} onClick={onConfirm} loading={loading}>
            {confirmLabel}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
