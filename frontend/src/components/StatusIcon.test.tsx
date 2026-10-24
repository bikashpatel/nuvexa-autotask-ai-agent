import { describe, expect, it } from 'vitest';

import { renderWithProviders } from '../test/utils';
import type { StatusIconName } from '../theme/status';
import { StatusIcon } from './StatusIcon';

const ICON_NAMES = [
  'clock',
  'check',
  'loader',
  'circle-check',
  'alert',
  'help',
  'x',
  'ban',
  'layers-off',
  'circle',
  'inbox',
  'plug',
  'plug-off',
] as const satisfies readonly StatusIconName[];

describe('<StatusIcon>', () => {
  it.each(ICON_NAMES)('renders a decorative svg for %s', (name) => {
    const { container } = renderWithProviders(<StatusIcon name={name} />);
    const svg = container.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    expect(svg).toHaveAttribute('width', '12');
    expect(svg).toHaveAttribute('stroke-width', '2');
    expect(container.querySelector('.mantine-Loader-root')).toBeNull();
  });

  it('renders distinct icons for distinct names', () => {
    // "loader" deliberately reuses the clock glyph when it is not animating.
    const shapes = new Set(
      ICON_NAMES.filter((n) => n !== 'loader').map((name) => {
        const svg = renderWithProviders(<StatusIcon name={name} />).container.querySelector('svg')!;
        return Array.from(svg.querySelectorAll('path'), (p) => p.getAttribute('d')).join('|');
      }),
    );
    expect(shapes.size).toBe(ICON_NAMES.length - 1);
  });

  it('honours the size prop', () => {
    const { container } = renderWithProviders(<StatusIcon name="check" size={20} />);
    expect(container.querySelector('svg')).toHaveAttribute('width', '20');
  });

  it('renders an aria-hidden loader instead of the icon while loading', () => {
    const { container } = renderWithProviders(<StatusIcon name="loader" loading color="cyan" />);
    const loader = container.querySelector('.mantine-Loader-root');
    expect(loader).not.toBeNull();
    expect(loader).toHaveAttribute('aria-hidden', 'true');
    expect(container.querySelector('svg')).toBeNull();
  });
});
