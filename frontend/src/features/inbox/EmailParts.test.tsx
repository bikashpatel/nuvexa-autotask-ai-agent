import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../../test/axe';
import { XSS_IMG, XSS_SCRIPT, XSS_TEXT, emailDetail } from '../../test/fixtures';
import { renderWithProviders } from '../../test/utils';
import { EmailBody, EmailHeaders } from './EmailParts';

function valueOf(label: string): HTMLElement {
  const term = screen.getByText(label);
  const item = term.parentElement!;
  return item.lastElementChild as HTMLElement;
}

describe('<EmailHeaders>', () => {
  it('lists sender, recipients, received, importance, message id and an Outlook link', async () => {
    const { container } = renderWithProviders(
      <EmailHeaders
        email={emailDetail({
          to_recipients: [
            { name: 'Support', address: 'support@contoso.example' },
            { name: null, address: 'ops@contoso.example' },
          ],
          cc_recipients: [{ name: 'Lee', address: null }],
          importance: 'high',
        })}
      />,
    );
    expect(valueOf('From')).toHaveTextContent('Dana Customer <dana@fabrikam.example>');
    expect(valueOf('To')).toHaveTextContent('Support <support@contoso.example>, ops@contoso.example');
    expect(valueOf('Cc')).toHaveTextContent('Lee');
    expect(valueOf('Received')).toHaveTextContent(/Oct 24, 2026/);
    expect(valueOf('Importance')).toHaveTextContent('High');
    expect(valueOf('Message ID')).toHaveTextContent('<abc@fabrikam.example>');

    const link = screen.getByRole('link', { name: /Outlook on the web/ });
    expect(link).toHaveAttribute('href', 'https://outlook.office.com/mail/id/abc');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(container.querySelector('dl')).not.toBeNull();
    await expectNoSeriousA11yViolations(container);
  });

  it('hides Cc, importance, message id and Outlook rows when absent; shows fallbacks', () => {
    renderWithProviders(
      <EmailHeaders
        email={emailDetail({
          sender_name: null,
          sender_address: null,
          to_recipients: [],
          cc_recipients: [],
          importance: null,
          internet_message_id: null,
          web_link: null,
        })}
      />,
    );
    expect(valueOf('From')).toHaveTextContent('Unknown sender');
    expect(valueOf('To')).toHaveTextContent('—');
    expect(screen.queryByText('Cc')).toBeNull();
    expect(screen.queryByText('Importance')).toBeNull();
    expect(screen.queryByText('Message ID')).toBeNull();
    expect(screen.queryByText('Open in Outlook')).toBeNull();
  });

  it.each(['mock://outlook/abc', 'javascript:alert(1)', 'data:text/html,<b>x</b>'])(
    'never renders a non-http web_link (%s) as a link',
    (webLink) => {
      renderWithProviders(<EmailHeaders email={emailDetail({ web_link: webLink })} />);
      expect(screen.queryByRole('link')).toBeNull();
      expect(screen.getByText(webLink)).toBeInTheDocument();
    },
  );

  it('renders hostile sender names as text', () => {
    const { container } = renderWithProviders(
      <EmailHeaders email={emailDetail({ sender_name: XSS_IMG, sender_address: null })} />,
    );
    expect(screen.getByText(XSS_IMG)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });
});

describe('<EmailBody>', () => {
  it('renders the body as plain text: no element is created from HTML-looking input', () => {
    const { container } = renderWithProviders(<EmailBody email={emailDetail({ body_text: XSS_TEXT })} />);
    expect(screen.getByText(XSS_TEXT)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect((window as unknown as { __nuvexaXss?: number }).__nuvexaXss).toBeUndefined();
    expect(screen.getByText(XSS_TEXT)).toHaveClass('nuvexa-plain-text');
  });

  it('falls back to the preview, then to "(empty body)"', () => {
    const { unmount } = renderWithProviders(
      <EmailBody email={emailDetail({ body_text: null, body_preview: `Preview ${XSS_SCRIPT}` })} />,
    );
    expect(screen.getByText(`Preview ${XSS_SCRIPT}`)).toBeInTheDocument();
    unmount();
    renderWithProviders(<EmailBody email={emailDetail({ body_text: '', body_preview: '' })} />);
    expect(screen.getByText('(empty body)')).toBeInTheDocument();
  });

  it('explains a body purged by retention instead of showing text', () => {
    renderWithProviders(<EmailBody email={emailDetail({ body_purged: true, body_text: 'secret' })} />);
    expect(screen.getByText('Body purged by retention policy')).toBeInTheDocument();
    expect(screen.queryByText('secret')).toBeNull();
  });
});
