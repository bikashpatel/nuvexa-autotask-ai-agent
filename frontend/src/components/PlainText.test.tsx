import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { XSS_IMG, XSS_SCRIPT, XSS_TEXT } from '../test/fixtures';
import { renderWithProviders } from '../test/utils';
import { PlainText } from './PlainText';

const win = window as unknown as Record<string, unknown>;

afterEach(() => {
  delete win.__nuvexaXss;
});

describe('<PlainText>', () => {
  it('renders a pre-wrap block (div) by default and a span when inline', () => {
    const { container, unmount } = renderWithProviders(<PlainText>Body</PlainText>);
    const block = container.querySelector('.nuvexa-plain-text')!;
    expect(block.tagName).toBe('DIV');
    expect(block).toHaveTextContent('Body');
    unmount();
    const inline = renderWithProviders(<PlainText inline>Comment</PlainText>);
    expect(inline.container.querySelector('.nuvexa-plain-text')!.tagName).toBe('SPAN');
  });

  it.each([null, undefined, ''])('renders an empty element for %j', (value) => {
    const { container } = renderWithProviders(<PlainText>{value}</PlainText>);
    expect(container.querySelector('.nuvexa-plain-text')).toBeEmptyDOMElement();
  });

  it('renders hostile email/AI text as visible text: no element is created and nothing executes', () => {
    const { container } = renderWithProviders(<PlainText>{XSS_TEXT}</PlainText>);
    const block = container.querySelector('.nuvexa-plain-text')!;
    expect(block.textContent).toBe(XSS_TEXT);
    expect(screen.getByText(XSS_TEXT)).toBeVisible();
    expect(block.children).toHaveLength(0);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(win.__nuvexaXss).toBeUndefined();
  });

  it.each([XSS_IMG, XSS_SCRIPT, '<a href="javascript:alert(1)">click</a>', '**bold** [link](https://x.y)'])(
    'does not interpret %j as HTML, links or markdown',
    (value) => {
      const { container } = renderWithProviders(<PlainText>{value}</PlainText>);
      expect(container.querySelector('.nuvexa-plain-text')!.textContent).toBe(value);
      expect(container.querySelector('a, img, script, strong')).toBeNull();
    },
  );

  it('preserves newlines in the text content', () => {
    const body = 'Hi team,\n\nCheckout returns HTTP 500.\n  — Dana';
    const { container } = renderWithProviders(<PlainText>{body}</PlainText>);
    expect(container.querySelector('.nuvexa-plain-text')!.textContent).toBe(body);
  });

  it('forwards Box props', () => {
    renderWithProviders(
      <PlainText data-testid="pt" c="dimmed">
        x
      </PlainText>,
    );
    expect(screen.getByTestId('pt')).toHaveClass('nuvexa-plain-text');
  });
});
