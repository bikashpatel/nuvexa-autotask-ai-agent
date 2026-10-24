import { ActionIcon, Tooltip, useComputedColorScheme, useMantineColorScheme } from '@mantine/core';
import { IconMoon, IconSun } from '@tabler/icons-react';

export function ColorSchemeToggle() {
  const { setColorScheme } = useMantineColorScheme();
  const computed = useComputedColorScheme('light', { getInitialValueInEffect: true });
  const next = computed === 'dark' ? 'light' : 'dark';
  const label = `Switch to ${next} theme`;
  return (
    <Tooltip label={label}>
      <ActionIcon variant="default" size="lg" onClick={() => setColorScheme(next)} aria-label={label}>
        {computed === 'dark' ? <IconSun size={18} aria-hidden /> : <IconMoon size={18} aria-hidden />}
      </ActionIcon>
    </Tooltip>
  );
}
