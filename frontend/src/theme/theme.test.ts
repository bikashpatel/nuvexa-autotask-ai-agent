import { describe, expect, it } from 'vitest';

import { ACTION_STATUS } from './status';
import { theme } from './theme';

const HEX = /^#[0-9a-f]{6}$/;

function defaultProps(component: string): Record<string, unknown> {
  const entry: { defaultProps?: Record<string, unknown> } | undefined = theme.components?.[component];
  return entry?.defaultProps ?? {};
}

describe('Mantine theme', () => {
  it('uses the NUVEXA brand colour as primary, distinct from the "approved" status blue', () => {
    expect(theme.primaryColor).toBe('nuvexa');
    expect(theme.primaryColor).not.toBe(ACTION_STATUS.approved.color);
    expect(theme.primaryShade).toEqual({ light: 6, dark: 4 });
  });

  it.each(['nuvexa', 'lagoon'])('%s is a 10-shade hex tuple', (name) => {
    const tuple = theme.colors?.[name];
    expect(tuple).toHaveLength(10);
    for (const shade of tuple ?? []) expect(shade).toMatch(HEX);
  });

  it('sets typography, radius and an accessible focus ring', () => {
    expect(theme.defaultRadius).toBe('md');
    expect(theme.focusRing).toBe('auto');
    expect(theme.cursorType).toBe('pointer');
    expect(theme.fontFamily).toMatch(/^Inter,/);
    expect(theme.fontFamilyMonospace).toMatch(/monospace$/);
    expect(theme.headings?.fontWeight).toBe('650');
    expect(Object.keys(theme.headings?.sizes ?? {})).toEqual(['h1', 'h2', 'h3', 'h4']);
  });

  it('defines component defaults', () => {
    expect(defaultProps('Card')).toEqual({ withBorder: true, radius: 'md', padding: 'lg' });
    expect(defaultProps('Paper')).toEqual({ radius: 'md' });
    expect(defaultProps('Button')).toEqual({ radius: 'md' });
    expect(defaultProps('ActionIcon')).toEqual({ variant: 'subtle', radius: 'md' });
    expect(defaultProps('Badge')).toEqual({ radius: 'sm', tt: 'none' });
    expect(defaultProps('Tooltip')).toEqual({ withArrow: true, openDelay: 150, multiline: true, maw: 320 });
    expect(defaultProps('Modal')).toEqual({
      centered: true,
      radius: 'md',
      closeButtonProps: { 'aria-label': 'Close dialog' },
    });
    expect(defaultProps('Alert')).toEqual({ radius: 'md' });
    expect(defaultProps('Table')).toEqual({ verticalSpacing: 'sm', highlightOnHover: true });
  });
});
