import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { ErrorState } from './ErrorState';

function apiError(status: number, code: string, correlationId: string | null = `corr-${code}`) {
  return new ApiError({
    status,
    code,
    message: `Server says ${code}`,
    correlationId,
    requestId: 'req-123',
  });
}

function alertColor(): string {
  return screen.getByRole('alert').getAttribute('style') ?? '';
}

describe('<ErrorState>', () => {
  it('renders a generic Error message without a support reference', async () => {
    renderWithProviders(<ErrorState error={new Error('Boom')} />);
    const alert = screen.getByRole('alert', { name: 'Something went wrong' });
    expect(alert).toHaveTextContent('Boom');
    expect(within(alert).queryByText('Support reference')).toBeNull();
    expect(alertColor()).toContain('--mantine-color-red-light');
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    await expectNoSeriousA11yViolations();
  });

  it('renders a fallback message for non-Error values', () => {
    renderWithProviders(<ErrorState error="nope" />);
    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong.');
  });

  it('shows the correlation id as the support reference with code and HTTP status', async () => {
    renderWithProviders(<ErrorState error={apiError(500, 'internal_error', 'corr-77')} />);
    const alert = screen.getByRole('alert', { name: 'Something went wrong' });
    expect(alert).toHaveTextContent('Server says internal_error');
    expect(within(alert).getByText('Support reference')).toBeInTheDocument();
    expect(within(alert).getByText('corr-77')).toBeInTheDocument();
    expect(within(alert).getByText('(internal_error, HTTP 500)')).toBeInTheDocument();
    await expectNoSeriousA11yViolations();
  });

  it('falls back to the request id when there is no correlation id', () => {
    renderWithProviders(<ErrorState error={apiError(502, 'http_502', null)} />);
    expect(screen.getByText('req-123')).toBeInTheDocument();
  });

  it('explains 404 without leaking the server message', () => {
    renderWithProviders(<ErrorState error={apiError(404, 'not_found')} />);
    const alert = screen.getByRole('alert', { name: 'Not found' });
    expect(alert).toHaveTextContent('It may have been removed, or it is not visible to your account.');
    expect(alert).not.toHaveTextContent('Server says not_found');
    expect(within(alert).getByText('corr-not_found')).toBeInTheDocument();
    expect(alertColor()).toContain('--mantine-color-gray-light');
  });

  it('explains 403', () => {
    renderWithProviders(<ErrorState error={apiError(403, 'permission_denied')} />);
    const alert = screen.getByRole('alert', { name: 'You do not have access to this' });
    expect(alert).toHaveTextContent('Server says permission_denied');
    expect(alertColor()).toContain('--mantine-color-orange-light');
  });

  it('explains network failures (status 0 → HTTP n/a)', () => {
    renderWithProviders(
      <ErrorState
        error={
          new ApiError({
            status: 0,
            code: 'network_error',
            message: 'Could not reach the NUVEXA API.',
            requestId: 'req-net',
          })
        }
      />,
    );
    const alert = screen.getByRole('alert', { name: 'API unreachable' });
    expect(alert).toHaveTextContent('Could not reach the NUVEXA API.');
    expect(within(alert).getByText('req-net')).toBeInTheDocument();
    expect(within(alert).getByText('(network_error, HTTP n/a)')).toBeInTheDocument();
    expect(alertColor()).toContain('--mantine-color-red-light');
  });

  it.each([
    [apiError(404, 'not_found'), 'Email not found'],
    [apiError(403, 'permission_denied'), 'No access'],
    [apiError(0, 'network_error'), 'Offline'],
    [new Error('x'), 'Could not load'],
  ])('lets a custom title override the default heading (%#)', (error, title) => {
    renderWithProviders(<ErrorState error={error} title={title} />);
    expect(screen.getByRole('alert', { name: title })).toBeInTheDocument();
  });

  it('retries on click and from the keyboard (Enter / Space)', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    renderWithProviders(<ErrorState error={apiError(500, 'internal_error')} onRetry={onRetry} />);
    const retry = screen.getByRole('button', { name: 'Retry' });
    await user.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
    retry.focus();
    await user.keyboard('{Enter}');
    await user.keyboard(' ');
    expect(onRetry).toHaveBeenCalledTimes(3);
    await expectNoSeriousA11yViolations();
  });

  it('shows a loading, disabled retry button while retrying', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    renderWithProviders(<ErrorState error={new Error('x')} onRetry={onRetry} retrying />);
    const retry = screen.getByRole('button', { name: 'Retry' });
    expect(retry).toHaveAttribute('data-loading');
    expect(retry).toBeDisabled();
    await user.click(retry);
    expect(onRetry).not.toHaveBeenCalled();
  });

  it('uses tighter padding when compact', () => {
    const { unmount } = renderWithProviders(<ErrorState error={new Error('x')} />);
    expect(screen.getByRole('alert').style.padding).toBe('var(--mantine-spacing-md)');
    unmount();
    renderWithProviders(<ErrorState error={new Error('x')} compact />);
    expect(screen.getByRole('alert').style.padding).toBe('var(--mantine-spacing-sm)');
  });
});
