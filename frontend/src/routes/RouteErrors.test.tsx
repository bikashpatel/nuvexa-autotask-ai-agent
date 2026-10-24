import { MantineProvider } from '@mantine/core';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import { expectNoSeriousA11yViolations } from '../test/axe';
import { theme } from '../theme/theme';
import { NotFoundPage, RouteErrorBoundary } from './RouteErrors';

function renderWithBoundary(failure: { loader?: () => never; render?: () => never }) {
  function Thrower(): never {
    return failure.render!();
  }
  const router = createMemoryRouter(
    [
      {
        path: '/',
        loader: failure.loader,
        element: failure.render ? <Thrower /> : <p>never</p>,
        ErrorBoundary: RouteErrorBoundary,
      },
    ],
    { initialEntries: ['/'] },
  );
  render(
    <MantineProvider theme={theme} env="test">
      <RouterProvider router={router} />
    </MantineProvider>,
  );
  return router;
}

beforeEach(() => {
  // React and React Router log caught render errors; keep the output readable.
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  vi.spyOn(console, 'warn').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('<NotFoundPage>', () => {
  it('explains the missing page and links back to the overview', async () => {
    const router = createMemoryRouter([{ path: '*', element: <NotFoundPage /> }], {
      initialEntries: ['/nope'],
    });
    render(
      <MantineProvider theme={theme} env="test">
        <RouterProvider router={router} />
      </MantineProvider>,
    );
    expect(screen.getByText('Page not found')).toBeInTheDocument();
    expect(screen.getByText('The page you are looking for does not exist or has moved.')).toBeInTheDocument();
    const back = screen.getByRole('link', { name: 'Back to overview' });
    expect(back).toHaveAttribute('href', '/');
    await userEvent.setup().click(back);
    expect(router.state.location.pathname).toBe('/');
    await expectNoSeriousA11yViolations();
  });
});

describe('<RouteErrorBoundary>', () => {
  it('shows "status statusText" for a thrown route error response', async () => {
    renderWithBoundary({
      loader: () => {
        // React Router's documented way to raise an HTTP-style route error is throwing a Response.
        // eslint-disable-next-line @typescript-eslint/only-throw-error
        throw new Response('', { status: 404, statusText: 'Not Found' });
      },
    });
    expect(await screen.findByRole('heading', { name: '404 Not Found' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong');
    expect(screen.getByRole('button', { name: 'Reload page' })).toBeInTheDocument();
  });

  it.each([
    'Failed to fetch dynamically imported module: https://app.example/assets/InboxPage-3f2a.js',
    'Importing a module script failed.',
  ])('asks for a reload after a deployment when a lazy chunk fails (%s)', async (message) => {
    renderWithBoundary({
      render: () => {
        throw new TypeError(message);
      },
    });
    expect(await screen.findByRole('heading', { name: 'Unexpected error' })).toBeInTheDocument();
    expect(
      screen.getByText('A new version of NUVEXA was deployed. Reload the page to continue.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders a generic error through ErrorState with its message', async () => {
    renderWithBoundary({
      render: () => {
        throw new Error('Cannot read properties of undefined');
      },
    });
    expect(await screen.findByRole('heading', { name: 'Unexpected error' })).toBeInTheDocument();
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Something went wrong');
    expect(alert).toHaveTextContent('Cannot read properties of undefined');
    expect(alert).not.toHaveTextContent('Support reference');
  });

  it('shows the support reference for an ApiError thrown by a loader', async () => {
    renderWithBoundary({
      loader: () => {
        throw new ApiError({
          status: 500,
          code: 'internal_error',
          message: 'Unexpected server error.',
          correlationId: 'corr-route-1',
          requestId: 'req-1',
        });
      },
    });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Unexpected server error.');
    expect(alert).toHaveTextContent('Support reference');
    expect(alert).toHaveTextContent('corr-route-1');
    expect(alert).toHaveTextContent('internal_error, HTTP 500');
    await expectNoSeriousA11yViolations();
  });

  it('"Reload page" reloads the browser page', async () => {
    const user = userEvent.setup();
    const reload = vi.fn();
    const original = window.location;
    Object.defineProperty(window, 'location', { configurable: true, value: { ...original, reload } });
    try {
      renderWithBoundary({
        render: () => {
          throw new Error('boom');
        },
      });
      await user.click(await screen.findByRole('button', { name: 'Reload page' }));
      expect(reload).toHaveBeenCalledTimes(1);
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original });
    }
  });
});
