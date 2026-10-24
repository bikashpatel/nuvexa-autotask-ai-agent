import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { queryKeys } from '../../api/queryKeys';
import type { ActionDetail, ActionPayload } from '../../api/types';
import { HASH_V4, actionSummary, adoAction, auditEvent } from '../../test/fixtures';
import { callsTo, createWrapper, jsonResponse, mockFetch } from '../../test/utils';
import {
  useActionDetail,
  useActionHistory,
  useApproveAction,
  useCancelAction,
  useExecuteAction,
  useReconcileAction,
  useRejectAction,
  useUpdateAction,
} from './hooks';

const DETAIL = '/api/v1/actions/act-1';

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function expectRelatedInvalidated(spy: { mock: { calls: unknown[][] } }) {
  const keys = spy.mock.calls.map((c) => (c[0] as { queryKey: unknown }).queryKey);
  expect(keys).toEqual(
    expect.arrayContaining([
      queryKeys.workflow('run-1'),
      ['actions', 'list'],
      queryKeys.actionHistory('act-1'),
      queryKeys.dashboard,
      queryKeys.email('email-1'),
    ]),
  );
}

describe('useActionDetail', () => {
  it('fetches GET /actions/{id} and is disabled without an id', async () => {
    const { calls } = mockFetch([{ method: 'GET', path: DETAIL, handler: () => jsonResponse(adoAction()) }]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useActionDetail('act-1'), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.id).toBe('act-1');
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(1);

    const { result: disabled } = renderHook(() => useActionDetail(''), { wrapper: Wrapper });
    expect(disabled.current.fetchStatus).toBe('idle');
    expect(calls).toHaveLength(1);
  });

  it('polls every 2 s while the action is executing and stops once it is terminal', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status: ActionDetail['status'] = 'executing';
    const { calls } = mockFetch([
      { method: 'GET', path: DETAIL, handler: () => jsonResponse(adoAction({ status })) },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useActionDetail('act-1'), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.data?.status).toBe('executing'));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(1);

    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(2);

    status = 'completed';
    await act(() => vi.advanceTimersByTimeAsync(2000));
    await waitFor(() => expect(result.current.data?.status).toBe('completed'));
    const settled = callsTo(calls, 'GET', DETAIL).length;
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(settled);
  });

  it('does not poll a non-executing action without a watch', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = mockFetch([
      { method: 'GET', path: DETAIL, handler: () => jsonResponse(adoAction({ status: 'approved' })) },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useActionDetail('act-1'), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(1);
  });

  it('a queued watch polls while updated_at is unchanged, and stops once the server state changes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let updatedAt = '2026-10-24T10:00:00Z';
    const { calls } = mockFetch([
      {
        method: 'GET',
        path: DETAIL,
        handler: () => jsonResponse(adoAction({ status: 'approved', updated_at: updatedAt })),
      },
    ]);
    const { Wrapper } = createWrapper();
    const watch = { updatedAt: '2026-10-24T10:00:00Z', until: Date.now() + 120_000 };
    const { result } = renderHook(() => useActionDetail('act-1', { watch }), { wrapper: Wrapper });
    // Read `data` so the tracked query result re-renders on data changes.
    await waitFor(() => expect(result.current.data?.updated_at).toBe('2026-10-24T10:00:00Z'));
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(2);

    updatedAt = '2026-10-24T10:00:09Z';
    await act(() => vi.advanceTimersByTimeAsync(2000));
    await waitFor(() => expect(result.current.data?.updated_at).toBe('2026-10-24T10:00:09Z'));
    const settled = callsTo(calls, 'GET', DETAIL).length;
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(settled);
  });

  it('a queued watch stops polling at its deadline even if nothing changed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = mockFetch([
      { method: 'GET', path: DETAIL, handler: () => jsonResponse(adoAction({ status: 'approved' })) },
    ]);
    const { Wrapper } = createWrapper();
    const watch = { updatedAt: '2026-10-24T10:00:00Z', until: Date.now() + 5_000 };
    const { result } = renderHook(() => useActionDetail('act-1', { watch }), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await act(() => vi.advanceTimersByTimeAsync(8_000));
    const atDeadline = callsTo(calls, 'GET', DETAIL).length;
    expect(atDeadline).toBeGreaterThanOrEqual(3);
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(callsTo(calls, 'GET', DETAIL)).toHaveLength(atDeadline);
  });
});

describe('action mutations (never optimistic: the cache only receives the server response)', () => {
  it('useUpdateAction PATCHes expected_version + payload and stores the returned detail', async () => {
    const saved = adoAction({ version: 4, payload_hash: HASH_V4 });
    const { calls } = mockFetch([{ method: 'PATCH', path: DETAIL, handler: () => jsonResponse(saved) }]);
    const { Wrapper, queryClient } = createWrapper();
    queryClient.setQueryData(queryKeys.action('act-1'), adoAction());
    const spy = vi.spyOn(queryClient, 'invalidateQueries');
    const { result } = renderHook(() => useUpdateAction('act-1'), { wrapper: Wrapper });
    const payload = adoAction().payload as unknown as ActionPayload;
    await act(() => result.current.mutateAsync({ expectedVersion: 3, payload }));
    expect(callsTo(calls, 'PATCH', DETAIL)[0]!.body).toEqual({ expected_version: 3, payload });
    expect(queryClient.getQueryData<ActionDetail>(queryKeys.action('act-1'))).toEqual(saved);
    expectRelatedInvalidated(spy);
  });

  it('useApproveAction sends version, payload_hash and a trimmed comment (blank → null)', async () => {
    const approved = adoAction({ status: 'approved' });
    const { calls } = mockFetch([
      { method: 'POST', path: `${DETAIL}/approve`, handler: () => jsonResponse(approved) },
    ]);
    const { Wrapper, queryClient } = createWrapper();
    const spy = vi.spyOn(queryClient, 'invalidateQueries');
    const { result } = renderHook(() => useApproveAction('act-1'), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync({ version: 7, payloadHash: HASH_V4, comment: '  ok  ' }));
    await act(() => result.current.mutateAsync({ version: 7, payloadHash: HASH_V4, comment: '   ' }));
    await act(() => result.current.mutateAsync({ version: 7, payloadHash: HASH_V4 }));
    const bodies = callsTo(calls, 'POST', `${DETAIL}/approve`).map((c) => c.body);
    expect(bodies).toEqual([
      { version: 7, payload_hash: HASH_V4, comment: 'ok' },
      { version: 7, payload_hash: HASH_V4, comment: null },
      { version: 7, payload_hash: HASH_V4, comment: null },
    ]);
    expect(queryClient.getQueryData<ActionDetail>(queryKeys.action('act-1'))?.status).toBe('approved');
    expectRelatedInvalidated(spy);
  });

  it('useRejectAction sends the trimmed reason', async () => {
    const { calls } = mockFetch([
      {
        method: 'POST',
        path: `${DETAIL}/reject`,
        handler: () => jsonResponse(adoAction({ status: 'rejected' })),
      },
    ]);
    const { Wrapper, queryClient } = createWrapper();
    const { result } = renderHook(() => useRejectAction('act-1'), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync('  Duplicate of #12  '));
    expect(callsTo(calls, 'POST', `${DETAIL}/reject`)[0]!.body).toEqual({ reason: 'Duplicate of #12' });
    expect(queryClient.getQueryData<ActionDetail>(queryKeys.action('act-1'))?.status).toBe('rejected');
  });

  it('useCancelAction sends the trimmed reason, or null when blank', async () => {
    const { calls } = mockFetch([
      {
        method: 'POST',
        path: `${DETAIL}/cancel`,
        handler: () => jsonResponse(adoAction({ status: 'cancelled' })),
      },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useCancelAction('act-1'), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync('  not needed '));
    await act(() => result.current.mutateAsync('   '));
    expect(callsTo(calls, 'POST', `${DETAIL}/cancel`).map((c) => c.body)).toEqual([
      { reason: 'not needed' },
      { reason: null },
    ]);
  });

  it('useExecuteAction sends version + payload_hash and merges the ActionOut into the cached detail', async () => {
    const before = adoAction({ status: 'approved', executions: [] });
    const accepted = actionSummary({ status: 'executing', updated_at: '2026-10-24T10:20:00Z' });
    const { calls } = mockFetch([
      {
        method: 'POST',
        path: `${DETAIL}/execute`,
        handler: () => jsonResponse({ action: accepted, job_id: 'job-1' }, 202),
      },
    ]);
    const { Wrapper, queryClient } = createWrapper();
    queryClient.setQueryData(queryKeys.action('act-1'), before);
    const spy = vi.spyOn(queryClient, 'invalidateQueries');
    const { result } = renderHook(() => useExecuteAction('act-1'), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync({ version: 7, payloadHash: HASH_V4 }));
    expect(callsTo(calls, 'POST', `${DETAIL}/execute`)[0]!.body).toEqual({
      version: 7,
      payload_hash: HASH_V4,
    });
    const cached = queryClient.getQueryData<ActionDetail>(queryKeys.action('act-1'));
    expect(cached?.status).toBe('executing');
    expect(cached?.updated_at).toBe('2026-10-24T10:20:00Z');
    // Detail-only fields survive the merge.
    expect(cached?.executions).toEqual([]);
    expect(cached?.approvals).toEqual([]);
    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.action('act-1'), exact: true });
    expectRelatedInvalidated(spy);
  });

  it('useReconcileAction POSTs without a body and does not invent a detail when none is cached', async () => {
    const { calls } = mockFetch([
      {
        method: 'POST',
        path: `${DETAIL}/reconcile`,
        handler: () => jsonResponse({ action: actionSummary({ status: 'completed' }), job_id: 'job-2' }, 202),
      },
    ]);
    const { Wrapper, queryClient } = createWrapper();
    const { result } = renderHook(() => useReconcileAction('act-1'), { wrapper: Wrapper });
    await act(() => result.current.mutateAsync());
    const call = callsTo(calls, 'POST', `${DETAIL}/reconcile`)[0]!;
    expect(call.body).toBeUndefined();
    expect(queryClient.getQueryData(queryKeys.action('act-1'))).toBeUndefined();
  });

  it('does not touch the cache when the server rejects the write', async () => {
    mockFetch([
      {
        method: 'POST',
        path: `${DETAIL}/approve`,
        handler: () =>
          jsonResponse(
            { error: { code: 'stale_version', message: 'stale', correlation_id: 'c', details: [] } },
            409,
          ),
      },
    ]);
    const { Wrapper, queryClient } = createWrapper();
    const original = adoAction();
    queryClient.setQueryData(queryKeys.action('act-1'), original);
    const { result } = renderHook(() => useApproveAction('act-1'), { wrapper: Wrapper });
    await act(() =>
      result.current.mutateAsync({ version: 3, payloadHash: original.payload_hash }).catch(() => undefined),
    );
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(queryClient.getQueryData(queryKeys.action('act-1'))).toBe(original);
  });
});

describe('useActionHistory', () => {
  it('fetches GET /actions/{id}/history and respects `enabled`', async () => {
    const { calls } = mockFetch([
      { method: 'GET', path: `${DETAIL}/history`, handler: () => jsonResponse([auditEvent()]) },
    ]);
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useActionHistory('act-1'), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.data).toHaveLength(1));

    const { result: off } = renderHook(() => useActionHistory('act-2', false), { wrapper: Wrapper });
    const { result: noId } = renderHook(() => useActionHistory(''), { wrapper: Wrapper });
    expect(off.current.fetchStatus).toBe('idle');
    expect(noId.current.fetchStatus).toBe('idle');
    expect(calls).toHaveLength(1);
  });
});
