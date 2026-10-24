import { notifications } from '@mantine/notifications';
import { act, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import { XSS_TEXT } from '../test/fixtures';
import { renderWithProviders } from '../test/utils';
import { errorMessage, notifyError, notifyInfo, notifySuccess } from './notify';

function apiError(overrides: Partial<ConstructorParameters<typeof ApiError>[0]> = {}) {
  return new ApiError({
    status: 409,
    code: 'stale_version',
    message: 'The resource was modified.',
    correlationId: 'corr-77',
    requestId: 'req-77',
    ...overrides,
  });
}

function mount() {
  return renderWithProviders(<p>page</p>, { withNotifications: true });
}

async function toast(title: string) {
  const heading = await screen.findByText(title);
  const root = heading.closest('[role="alert"]') ?? heading.parentElement!.parentElement!;
  return root as HTMLElement;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('errorMessage', () => {
  it('uses the ApiError / Error message, else a generic text', () => {
    expect(errorMessage(apiError())).toBe('The resource was modified.');
    expect(errorMessage(new Error('boom'))).toBe('boom');
    expect(errorMessage('a string')).toBe('Something went wrong.');
    expect(errorMessage(undefined)).toBe('Something went wrong.');
    expect(errorMessage({ message: 'not an error' })).toBe('Something went wrong.');
  });
});

describe('notifyError', () => {
  it('shows the title, message and "code · ref <correlation id>"', async () => {
    mount();
    act(() => notifyError('Approval failed', apiError()));
    const root = await toast('Approval failed');
    expect(within(root).getByText('The resource was modified.')).toBeInTheDocument();
    expect(within(root).getByText('stale_version · ref corr-77')).toBeInTheDocument();
  });

  it('falls back to the request id when there is no correlation id', async () => {
    mount();
    act(() =>
      notifyError('Save failed', apiError({ correlationId: null, code: 'http_502', requestId: 'req-9' })),
    );
    const root = await toast('Save failed');
    expect(within(root).getByText('http_502 · ref req-9')).toBeInTheDocument();
  });

  it('omits the code prefix when the code is empty', async () => {
    mount();
    act(() => notifyError('Odd failure', apiError({ code: '' })));
    const root = await toast('Odd failure');
    expect(within(root).getByText('ref corr-77')).toBeInTheDocument();
  });

  it('shows no support reference for plain errors and a generic text for unknown values', async () => {
    mount();
    act(() => notifyError('Plain failure', new Error('Disk full')));
    const plain = await toast('Plain failure');
    expect(within(plain).getByText('Disk full')).toBeInTheDocument();
    expect(within(plain).queryByText(/ref /)).toBeNull();

    act(() => notifyError('Unknown failure', 42));
    const unknown = await toast('Unknown failure');
    expect(within(unknown).getByText('Something went wrong.')).toBeInTheDocument();
  });

  it('renders hostile server messages as text', async () => {
    const { container } = mount();
    act(() => notifyError('Hostile', new Error(XSS_TEXT)));
    expect(await screen.findByText(XSS_TEXT)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(document.body.querySelector('img[src="x"]')).toBeNull();
  });

  it('is red, has an icon and stays for 10 s', () => {
    const show = vi.spyOn(notifications, 'show');
    notifyError('t', new Error('m'));
    expect(show).toHaveBeenCalledWith(
      expect.objectContaining({
        color: 'red',
        title: 't',
        autoClose: 10_000,
        icon: expect.anything() as unknown,
      }),
    );
  });
});

describe('notifySuccess / notifyInfo', () => {
  it('notifySuccess shows a green toast with an optional message', async () => {
    const show = vi.spyOn(notifications, 'show');
    mount();
    act(() => notifySuccess('Approved', 'Version 3 is approved.'));
    const root = await toast('Approved');
    expect(within(root).getByText('Version 3 is approved.')).toBeInTheDocument();
    act(() => notifySuccess('Saved'));
    expect(await screen.findByText('Saved')).toBeInTheDocument();
    expect(show).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ color: 'green', message: 'Version 3 is approved.' }),
    );
    expect(show).toHaveBeenNthCalledWith(2, expect.objectContaining({ color: 'green', message: '' }));
  });

  it('notifyInfo shows a blue toast with an optional message', async () => {
    const show = vi.spyOn(notifications, 'show');
    mount();
    act(() => notifyInfo('Analysis already in progress', 'An active run exists.'));
    const root = await toast('Analysis already in progress');
    expect(within(root).getByText('An active run exists.')).toBeInTheDocument();
    act(() => notifyInfo('Heads up'));
    expect(await screen.findByText('Heads up')).toBeInTheDocument();
    expect(show).toHaveBeenNthCalledWith(1, expect.objectContaining({ color: 'blue' }));
    expect(show).toHaveBeenNthCalledWith(2, expect.objectContaining({ color: 'blue', message: '' }));
  });
});
