import { describe, expect, it } from 'vitest';

import { XSS_TEXT } from '../test/fixtures';
import { renderWithProviders } from '../test/utils';
import { JsonBlock } from './JsonBlock';

function codeText(container: HTMLElement): string {
  return container.querySelector('pre')?.textContent ?? '';
}

describe('<JsonBlock>', () => {
  it('pretty-prints JSON with two-space indentation inside a code block', () => {
    const value = { version: 3, tags: ['checkout', 'eu'], nested: { ok: true } };
    const { container } = renderWithProviders(<JsonBlock value={value} />);
    expect(codeText(container)).toBe(JSON.stringify(value, null, 2));
    expect(container.querySelector('pre')).toHaveClass('mantine-Code-root');
  });

  it.each([
    ['undefined', undefined],
    ['a function', () => 1],
  ])('renders "null" for %s', (_, value) => {
    const { container } = renderWithProviders(<JsonBlock value={value} />);
    expect(codeText(container)).toBe('null');
  });

  it('falls back to String(value) when the value cannot be serialised', () => {
    const circular: Record<string, unknown> = { a: 1 };
    circular.self = circular;
    const { container, unmount } = renderWithProviders(<JsonBlock value={circular} />);
    expect(codeText(container)).toBe('[object Object]');
    unmount();
    const bigint = renderWithProviders(<JsonBlock value={BigInt(42)} />);
    expect(codeText(bigint.container)).toBe('42');
  });

  it('renders hostile strings as text, never as HTML', () => {
    const { container } = renderWithProviders(<JsonBlock value={{ title: XSS_TEXT }} />);
    expect(codeText(container)).toContain('<img src=x onerror=\\"window.__nuvexaXss=1\\">');
    expect(codeText(container)).toContain('<script>window.__nuvexaXss=1</script>');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });

  it('constrains the height with a scroll area', () => {
    const { container } = renderWithProviders(<JsonBlock value={{ a: 1 }} maxHeight={120} />);
    expect(container.querySelector('.mantine-ScrollArea-root')).not.toBeNull();
    // 120px → 7.5rem (Mantine converts px to rem).
    expect(container.querySelector<HTMLElement>('div')!.style.maxHeight).toContain('7.5rem');
  });
});
