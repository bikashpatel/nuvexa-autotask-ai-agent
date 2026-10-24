/**
 * Action queries and mutations. Approvals, rejections, executions and reconciliations are NEVER
 * applied optimistically: the cache is only updated with the server's confirmed response.
 */
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';

import { actionsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import type { ActionDetail, ActionOut, ActionPayload } from '../../api/types';
import { POLL_INTERVAL_MS, isActionInFlight } from '../../theme/status';

/**
 * Execute/reconcile are queued jobs: the 202 response still shows the pre-job state until a worker
 * picks the job up. A watch keeps polling while the action is unchanged since queueing (same
 * `updated_at`), bounded by `until`.
 */
export interface QueuedWatch {
  updatedAt: string;
  until: number;
}

export function useActionDetail(actionId: string, options: { watch?: QueuedWatch | null } = {}) {
  const { watch = null } = options;
  return useQuery({
    queryKey: queryKeys.action(actionId),
    queryFn: () => actionsApi.get(actionId),
    enabled: Boolean(actionId),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (isActionInFlight(data?.status)) return POLL_INTERVAL_MS;
      if (watch && data?.updated_at === watch.updatedAt && Date.now() < watch.until) return POLL_INTERVAL_MS;
      return false;
    },
  });
}

function invalidateRelated(
  queryClient: QueryClient,
  action: Pick<ActionOut, 'id' | 'workflow_run_id' | 'email_message_id'>,
) {
  void queryClient.invalidateQueries({ queryKey: queryKeys.workflow(action.workflow_run_id) });
  void queryClient.invalidateQueries({ queryKey: ['actions', 'list'] });
  void queryClient.invalidateQueries({ queryKey: queryKeys.actionHistory(action.id) });
  void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
  void queryClient.invalidateQueries({ queryKey: queryKeys.email(action.email_message_id) });
}

/** Store the confirmed server state (full detail) in the cache. */
function storeDetail(queryClient: QueryClient, detail: ActionDetail) {
  queryClient.setQueryData(queryKeys.action(detail.id), detail);
  invalidateRelated(queryClient, detail);
}

/** Execute/reconcile return ActionOut (no approvals/executions) — merge, then refetch the detail. */
function storeSummary(queryClient: QueryClient, action: ActionOut) {
  queryClient.setQueryData<ActionDetail>(queryKeys.action(action.id), (prev) =>
    prev ? { ...prev, ...action } : undefined,
  );
  void queryClient.invalidateQueries({ queryKey: queryKeys.action(action.id), exact: true });
  invalidateRelated(queryClient, action);
}

export function useUpdateAction(actionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: { expectedVersion: number; payload: ActionPayload }) =>
      actionsApi.update(actionId, { expected_version: vars.expectedVersion, payload: vars.payload }),
    onSuccess: (detail) => storeDetail(queryClient, detail),
  });
}

export function useApproveAction(actionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: { version: number; payloadHash: string; comment?: string }) =>
      actionsApi.approve(actionId, {
        version: vars.version,
        payload_hash: vars.payloadHash,
        comment: vars.comment?.trim() ? vars.comment.trim() : null,
      }),
    onSuccess: (detail) => storeDetail(queryClient, detail),
  });
}

export function useRejectAction(actionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (reason: string) => actionsApi.reject(actionId, { reason: reason.trim() }),
    onSuccess: (detail) => storeDetail(queryClient, detail),
  });
}

export function useCancelAction(actionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (reason: string) => actionsApi.cancel(actionId, { reason: reason.trim() || null }),
    onSuccess: (detail) => storeDetail(queryClient, detail),
  });
}

export function useExecuteAction(actionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: { version: number; payloadHash: string }) =>
      actionsApi.execute(actionId, { version: vars.version, payload_hash: vars.payloadHash }),
    onSuccess: (accepted) => storeSummary(queryClient, accepted.action),
  });
}

export function useReconcileAction(actionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => actionsApi.reconcile(actionId),
    onSuccess: (accepted) => storeSummary(queryClient, accepted.action),
  });
}

export function useActionHistory(actionId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.actionHistory(actionId),
    queryFn: () => actionsApi.history(actionId),
    enabled: Boolean(actionId) && enabled,
  });
}
