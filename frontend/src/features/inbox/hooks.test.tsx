import { notifications } from '@mantine/notifications';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { queryKeys } from '../../api/queryKeys';
import { workflowRun } from '../../test/fixtures';
import { callsTo, createWrapper, errorResponse, jsonResponse, mockFetch } from '../../test/utils';
import { useAnalyzeEmail, useReanalyzeWorkflow, useSyncMailbox } from './hooks';

function invalidatedKeys(spy: { mock: { calls: unknown[][] } }): unknown[] {
  return spy.mock.calls.map((call) => (call[0] as { queryKey: unknown }).queryKey);
}

let show: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  show = vi.spyOn(notifications, 'show');
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('useSyncMailbox', () => {
  it('POSTs an empty body without an account and a connected_account_id when given', async () => {
    const { calls } = mockFetch([
      {
        method: 'POST',
        path: '/api/v1/emails/sync',
        handler: () => jsonResponse({ job_ids: ['j-1', 'j-2'], message: 'Queued 2 jobs' }, 202),
      },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useSyncMailbox(), { wrapper: Wrapper });

    await act(() => result.current.mutateAsync(undefined));
    await act(() => result.current.mutateAsync('conn-9'));

    const posts = callsTo(calls, 'POST', '/api/v1/emails/sync');
    expect(posts.map((c) => c.body)).toEqual([{}, { connected_account_id: 'conn-9' }]);
    expect(show).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Mailbox sync queued (2 jobs)',
        message: 'Queued 2 jobs',
        color: 'green',
      }),
    );
  });

  it('titles a single job and an empty job list correctly', async () => {
    let jobs: string[] = ['j-1'];
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/emails/sync',
        handler: () => jsonResponse({ job_ids: jobs, message: 'ok' }, 202),
      },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useSyncMailbox(), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync(undefined));
    expect(show).toHaveBeenLastCalledWith(expect.objectContaining({ title: 'Mailbox sync queued (1 job)' }));
    jobs = [];
    await act(() => result.current.mutateAsync(undefined));
    expect(show).toHaveBeenLastCalledWith(expect.objectContaining({ title: 'Nothing to sync' }));
  });

  it('refreshes emails, integrations and the dashboard after 3 s and again after 8 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/emails/sync',
        handler: () => jsonResponse({ job_ids: ['j-1'], message: 'ok' }, 202),
      },
    ]);
    const { Wrapper, queryClient } = createWrapper();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    const { result } = renderHook(() => useSyncMailbox(), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync(undefined));

    expect(invalidate).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(invalidatedKeys(invalidate)).toEqual([
      queryKeys.emails,
      queryKeys.integrations,
      queryKeys.dashboard,
    ]);
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(invalidate).toHaveBeenCalledTimes(6);
  });

  it('clears pending refresh timers on unmount', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/emails/sync',
        handler: () => jsonResponse({ job_ids: ['j-1'], message: 'ok' }, 202),
      },
    ]);
    const { Wrapper, queryClient } = createWrapper();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    const { result, unmount } = renderHook(() => useSyncMailbox(), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync(undefined));
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(invalidate).not.toHaveBeenCalled();
  });

  it('shows an error toast carrying the support reference', async () => {
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/emails/sync',
        handler: () => errorResponse(503, 'mailbox_unavailable', { correlationId: 'corr-sync' }),
      },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useSyncMailbox(), { wrapper: Wrapper });
    act(() => result.current.mutate(undefined));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(show).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Could not start mailbox sync', color: 'red' }),
    );
  });
});

describe('useAnalyzeEmail', () => {
  it('starts an analysis, toasts success and invalidates the run, emails and workflows', async () => {
    const { calls } = mockFetch([
      {
        method: 'POST',
        path: '/api/v1/emails/email-1/analyze',
        handler: () =>
          jsonResponse({ workflow_run: workflowRun({ id: 'run-7' }), created: true, job_id: 'j-1' }, 202),
      },
    ]);
    const { Wrapper, queryClient } = createWrapper();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    const { result } = renderHook(() => useAnalyzeEmail(), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync('email-1'));

    expect(callsTo(calls, 'POST', '/api/v1/emails/email-1/analyze')).toHaveLength(1);
    expect(callsTo(calls, 'POST', '/api/v1/emails/email-1/analyze')[0]!.body).toBeUndefined();
    expect(show).toHaveBeenCalledWith(expect.objectContaining({ title: 'Analysis started', color: 'green' }));
    expect(invalidatedKeys(invalidate)).toEqual([
      queryKeys.workflow('run-7'),
      queryKeys.emails,
      queryKeys.email('email-1'),
      queryKeys.workflows,
    ]);
  });

  it('shows an info toast when a run is already active (created=false)', async () => {
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/emails/email-1/analyze',
        handler: () => jsonResponse({ workflow_run: workflowRun(), created: false, job_id: null }),
      },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useAnalyzeEmail(), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync('email-1'));
    expect(show).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Analysis already in progress', color: 'blue' }),
    );
  });

  it('toasts errors', async () => {
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/emails/email-1/analyze',
        handler: () => errorResponse(403, 'permission_denied'),
      },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useAnalyzeEmail(), { wrapper: Wrapper });
    act(() => result.current.mutate('email-1'));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(show).toHaveBeenCalledWith(expect.objectContaining({ title: 'Could not start analysis' }));
  });
});

describe('useReanalyzeWorkflow', () => {
  it('uses the server message, then invalidates the run, actions and emails', async () => {
    const { calls } = mockFetch([
      {
        method: 'POST',
        path: '/api/v1/workflows/run-1/reanalyze',
        handler: () => jsonResponse({ job_ids: ['j'], message: '2 proposals superseded' }, 202),
      },
    ]);
    const { Wrapper, queryClient } = createWrapper();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    const { result } = renderHook(() => useReanalyzeWorkflow(), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync('run-1'));
    expect(callsTo(calls, 'POST', '/api/v1/workflows/run-1/reanalyze')).toHaveLength(1);
    expect(show).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Re-analysis queued', message: '2 proposals superseded' }),
    );
    expect(invalidatedKeys(invalidate)).toEqual([
      queryKeys.workflow('run-1'),
      queryKeys.actions,
      queryKeys.emails,
    ]);
  });

  it('falls back to a default message when the server sends none', async () => {
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/workflows/run-1/reanalyze',
        handler: () => jsonResponse({ job_ids: [], message: '' }, 202),
      },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useReanalyzeWorkflow(), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync('run-1'));
    expect(show).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'Open proposals for this run will be superseded.' }),
    );
  });

  it('toasts errors', async () => {
    mockFetch([
      {
        method: 'POST',
        path: '/api/v1/workflows/run-1/reanalyze',
        handler: () => errorResponse(409, 'conflict'),
      },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useReanalyzeWorkflow(), { wrapper: Wrapper });
    act(() => result.current.mutate('run-1'));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(show).toHaveBeenCalledWith(expect.objectContaining({ title: 'Could not request re-analysis' }));
  });
});
