import { MantineProvider } from '@mantine/core';
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { theme } from '../../theme/theme';
import { BrandMark } from './BrandMark';

function renderMark(size?: number) {
  return render(
    <MantineProvider theme={theme} env="test">
      <BrandMark size={size} />
    </MantineProvider>,
  );
}

describe('<BrandMark>', () => {
  it('is decorative: hidden from assistive technology and not focusable', () => {
    const { container } = renderMark();
    const mark = container.querySelector('.nuvexa-brand-mark');
    expect(mark).not.toBeNull();
    expect(mark).toHaveAttribute('aria-hidden', 'true');
    expect(mark!.querySelector('svg')).not.toBeNull();
    expect(container.querySelector('a, button, [tabindex]')).toBeNull();
  });

  it('defaults to 32px with a ~56% glyph and a quarter-size radius', () => {
    const { container } = renderMark();
    const mark = container.querySelector<HTMLElement>('.nuvexa-brand-mark')!;
    expect(mark.style.borderRadius).toBe('8px');
    const svg = mark.querySelector('svg')!;
    expect(Number(svg.getAttribute('width'))).toBeCloseTo(32 * 0.56);
    expect(Number(svg.getAttribute('height'))).toBeCloseTo(32 * 0.56);
  });

  it('scales the glyph and radius with the size prop', () => {
    const { container } = renderMark(44);
    const mark = container.querySelector<HTMLElement>('.nuvexa-brand-mark')!;
    expect(mark.style.borderRadius).toBe('11px');
    expect(Number(mark.querySelector('svg')!.getAttribute('width'))).toBeCloseTo(44 * 0.56);
  });
});
