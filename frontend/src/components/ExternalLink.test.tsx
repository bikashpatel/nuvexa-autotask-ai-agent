import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { ExternalLink } from './ExternalLink';

describe('<ExternalLink>', () => {
  it.each([
    'https://dev.azure.com/contoso/Payments/_workitems/edit/4711',
    'http://intranet.contoso.example/wiki',
    'HTTPS://OUTLOOK.OFFICE.COM/mail/id/abc',
  ])('renders %s as a new-tab link with rel="noopener noreferrer"', async (url) => {
    renderWithProviders(<ExternalLink url={url}>Open work item</ExternalLink>);
    const link = screen.getByRole('link', { name: /Open work item/ });
    expect(link).toHaveAttribute('href', url);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(link).toHaveAccessibleName(/^Open work item ?\(opens in a new tab\)$/);
    expect(link.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    await expectNoSeriousA11yViolations();
  });

  it('uses the URL as link text when no children are given', () => {
    renderWithProviders(<ExternalLink url="https://status.contoso.example/" />);
    expect(screen.getByRole('link')).toHaveTextContent('https://status.contoso.example/');
  });

  it.each(['mock://contoso/Payments/_workitems/edit/4711', 'MOCK://contoso/x'])(
    'never links %s — shows it as text with a Mock badge',
    async (url) => {
      const { container } = renderWithProviders(<ExternalLink url={url}>Open</ExternalLink>);
      expect(container.querySelector('a')).toBeNull();
      expect(screen.queryByRole('link')).toBeNull();
      expect(screen.getByText(url).tagName).toBe('CODE');
      expect(screen.getByText('Mock')).toBeInTheDocument();
      expect(screen.queryByText('Open')).toBeNull();
      await expectNoSeriousA11yViolations();
    },
  );

  it.each([
    'javascript:alert(document.cookie)',
    ' JavaScript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'file:///etc/passwd',
    'vbscript:msgbox(1)',
    '/relative/path',
    '//evil.example/x',
    'ftp://files.example/x',
  ])('renders unsafe or non-http URL %j as plain text, never a link', (url) => {
    const { container } = renderWithProviders(<ExternalLink url={url}>Click me</ExternalLink>);
    expect(container.querySelector('a')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByText(url.trim())).toBeInTheDocument();
    expect(screen.queryByText('Mock')).toBeNull();
    expect(screen.queryByText('Click me')).toBeNull();
  });

  it.each([null, undefined, ''])('renders a dash for %j', (url) => {
    const { container } = renderWithProviders(<ExternalLink url={url} />);
    expect(container.querySelector('a')).toBeNull();
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
