import { QueryObserver } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import { createQueryClient } from './queryClient';

type RetryFn = (failureCount: number, error: unknown) => boolean;

function apiError(status: number) {
  return new ApiError({ status, code: `http_${status}`, message: 'x', requestId: 'r' });
}

function retryFn(): RetryFn {
  const retry = createQueryClient().getDefaultOptions().queries?.retry;
  expect(typeof retry).toBe('function');
  return retry as RetryFn;
}

afterEach(() => {
  vi.useRealTimers();
});

describe('createQueryClient defaults', () => {
  it('queries: 15 s stale time and refetch on window focus', () => {
    const queries = createQueryClient().getDefaultOptions().queries;
    expect(queries?.staleTime).toBe(15_000);
    expect(queries?.refetchOnWindowFocus).toBe(true);
  });

  it('mutations are never retried (approvals/executions must not be replayed)', () => {
    expect(createQueryClient().getDefaultOptions().mutations?.retry).toBe(false);
  });

  it.each([400, 401, 403, 404, 409, 422, 499])('does not retry a %s ApiError', (status) => {
    expect(retryFn()(0, apiError(status))).toBe(false);
  });

  it.each([
    ['500 ApiError', apiError(500)],
    ['503 ApiError', apiError(503)],
    ['network ApiError (status 0)', apiError(0)],
    ['plain Error', new Error('boom')],
    ['unknown value', 'boom'],
  ])('retries a %s at most twice', (_name, error) => {
    const retry = retryFn();
    expect(retry(0, error)).toBe(true);
    expect(retry(1, error)).toBe(true);
    expect(retry(2, error)).toBe(false);
  });

  it('end to end: a 404 is fetched once, a 500 three times', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const client = createQueryClient();

    const notFound = vi.fn().mockRejectedValue(apiError(404));
    const observer404 = new QueryObserver(client, { queryKey: ['x', 404], queryFn: notFound });
    const unsubscribe404 = observer404.subscribe(() => undefined);
    await vi.waitFor(() => expect(observer404.getCurrentResult().isError).toBe(true));
    expect(notFound).toHaveBeenCalledTimes(1);
    unsubscribe404();

    const serverError = vi.fn().mockRejectedValue(apiError(500));
    const observer500 = new QueryObserver(client, { queryKey: ['x', 500], queryFn: serverError });
    const unsubscribe500 = observer500.subscribe(() => undefined);
    await vi.advanceTimersByTimeAsync(10_000);
    await vi.waitFor(() => expect(observer500.getCurrentResult().isError).toBe(true));
    expect(serverError).toHaveBeenCalledTimes(3);
    unsubscribe500();
    client.clear();
  });
});
