import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';

import { emailsApi, workflowsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import { notifyError, notifyInfo, notifySuccess } from '../../lib/notify';

/** POST /emails/sync → toast with job count, then refetch the inbox after a few seconds. */
export function useSyncMailbox() {
  const queryClient = useQueryClient();
  const timers = useRef<number[]>([]);

  useEffect(() => {
    const pending = timers.current;
    return () => {
      pending.forEach((id) => window.clearTimeout(id));
    };
  }, []);

  return useMutation({
    mutationFn: (connectedAccountId?: string) =>
      emailsApi.sync(connectedAccountId ? { connected_account_id: connectedAccountId } : {}),
    onSuccess: (result) => {
      const count = result.job_ids.length;
      notifySuccess(
        count === 0 ? 'Nothing to sync' : `Mailbox sync queued (${count} job${count === 1 ? '' : 's'})`,
        result.message,
      );
      const refresh = () => {
        void queryClient.invalidateQueries({ queryKey: queryKeys.emails });
        void queryClient.invalidateQueries({ queryKey: queryKeys.integrations });
        void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
      };
      timers.current.push(window.setTimeout(refresh, 3000), window.setTimeout(refresh, 8000));
    },
    onError: (error) => notifyError('Could not start mailbox sync', error),
  });
}

/** POST /emails/{id}/analyze. `created=false` means a run is already active for the email. */
export function useAnalyzeEmail() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (emailId: string) => emailsApi.analyze(emailId),
    onSuccess: (result, emailId) => {
      if (result.created) {
        notifySuccess(
          'Analysis started',
          'The workflow is running. Results appear as soon as each step completes.',
        );
      } else {
        notifyInfo('Analysis already in progress', 'An active workflow run already exists for this email.');
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflow(result.workflow_run.id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.emails });
      void queryClient.invalidateQueries({ queryKey: queryKeys.email(emailId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflows });
    },
    onError: (error) => notifyError('Could not start analysis', error),
  });
}

/** POST /workflows/{id}/reanalyze — supersedes open proposals of that run. */
export function useReanalyzeWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (workflowId: string) => workflowsApi.reanalyze(workflowId),
    onSuccess: (result, workflowId) => {
      notifySuccess(
        'Re-analysis queued',
        result.message || 'Open proposals for this run will be superseded.',
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflow(workflowId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.actions });
      void queryClient.invalidateQueries({ queryKey: queryKeys.emails });
    },
    onError: (error) => notifyError('Could not request re-analysis', error),
  });
}
