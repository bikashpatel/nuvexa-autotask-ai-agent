import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { renderWithProviders } from '../test/utils';
import { InjectionWarning } from './InjectionWarning';

describe('<InjectionWarning>', () => {
  it('renders nothing when there are no flags', () => {
    renderWithProviders(<InjectionWarning flags={[]} />);
    expect(screen.queryByTestId('injection-warning')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders a prominent alert listing each indicator', () => {
    renderWithProviders(
      <InjectionWarning
        flags={[
          {
            rule: 'ignore_previous_instructions',
            severity: 'high',
            excerpt: 'Ignore all previous instructions',
          },
          { rule: 'auto_approval_request', severity: 'medium', excerpt: 'approve this automatically' },
        ]}
      />,
    );
    const alert = screen.getByTestId('injection-warning');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert).toHaveTextContent(
      /Possible prompt injection detected \(2 indicators, highest severity: high\)/,
    );
    expect(within(alert).getByText('Ignore previous instructions')).toBeInTheDocument();
    expect(within(alert).getByText('Auto approval request')).toBeInTheDocument();
    expect(within(alert).getByText('approve this automatically')).toBeInTheDocument();
    expect(alert).toHaveTextContent(/cannot approve or execute anything/);
  });

  it('renders excerpts as inert text, never as HTML', () => {
    const payload = '<img src=x onerror="alert(1)"><b>bold</b>';
    const { container } = renderWithProviders(
      <InjectionWarning flags={[{ rule: 'html_payload', severity: 'low', excerpt: payload }]} />,
    );
    expect(screen.getByText(payload)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
  });
});
