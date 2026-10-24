import { Alert, Button, Code, Group, Loader, Modal, Stack, Text, Textarea, Tooltip } from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import {
  IconBan,
  IconCheck,
  IconDeviceFloppy,
  IconPlayerPlay,
  IconRefreshDot,
  IconRotate,
  IconX,
} from '@tabler/icons-react';
import { useState } from 'react';

import { isApiError, type ApiError } from '../../api/client';
import type { ActionDetail } from '../../api/types';
import { actionTypeLabel, shortHash } from '../../lib/format';
import { notifyError, notifySuccess } from '../../lib/notify';
import { LIMITS } from './payloadForms';
import {
  useApproveAction,
  useCancelAction,
  useExecuteAction,
  useReconcileAction,
  useRejectAction,
} from './hooks';

const NO_PERMISSIONS = {
  can_edit: false,
  can_approve: false,
  can_reject: false,
  can_cancel: false,
  can_execute: false,
  can_reconcile: false,
};

export interface ActionControlsProps {
  action: ActionDetail;
  /** Unsaved edits in the payload form. */
  dirty: boolean;
  editable: boolean;
  saving: boolean;
  onSave: () => void;
  onDiscard: () => void;
  /** 409 stale_version from any write: the card shows a "changed — reload" banner. */
  onConflict: (error: ApiError) => void;
  /** Execute/reconcile job queued and not yet picked up (disables re-submission). */
  queued: boolean;
  /** Execute/reconcile were queued: keep polling until the server state changes. */
  onQueued: (updatedAt: string) => void;
}

function handleError(title: string, error: unknown, onConflict: (e: ApiError) => void) {
  if (isApiError(error) && error.isStaleVersion) {
    onConflict(error);
    return;
  }
  notifyError(title, error);
}

function HashLine({ action }: { action: ActionDetail }) {
  return (
    <Group gap="xs">
      <Text size="sm">Version</Text>
      <Code>v{action.version}</Code>
      <Text size="sm">payload</Text>
      <Code title={action.payload_hash}>{shortHash(action.payload_hash, 16)}</Code>
    </Group>
  );
}

export function ActionControls({
  action,
  dirty,
  editable,
  saving,
  onSave,
  onDiscard,
  onConflict,
  queued,
  onQueued,
}: ActionControlsProps) {
  const permissions = action.permissions ?? NO_PERMISSIONS;
  const approve = useApproveAction(action.id);
  const reject = useRejectAction(action.id);
  const cancel = useCancelAction(action.id);
  const execute = useExecuteAction(action.id);
  const reconcile = useReconcileAction(action.id);

  const [approveOpen, approveModal] = useDisclosure(false);
  const [rejectOpen, rejectModal] = useDisclosure(false);
  const [cancelOpen, cancelModal] = useDisclosure(false);
  const [executeOpen, executeModal] = useDisclosure(false);
  const [comment, setComment] = useState('');
  const [reason, setReason] = useState('');
  const [rejectTouched, setRejectTouched] = useState(false);
  const [cancelReason, setCancelReason] = useState('');

  const busy =
    queued ||
    approve.isPending ||
    reject.isPending ||
    cancel.isPending ||
    execute.isPending ||
    reconcile.isPending;
  const status = action.status;
  const isOpen = status === 'pending_approval' || status === 'approved';
  const approvalValid =
    action.current_approval !== null &&
    action.current_approval.decision === 'approved' &&
    !action.current_approval.invalidated_at &&
    action.current_approval.action_version === action.version &&
    action.current_approval.payload_hash === action.payload_hash;

  const showApprove = permissions.can_approve && status === 'pending_approval';
  const showReject = permissions.can_reject && isOpen;
  const showCancel = permissions.can_cancel && (isOpen || status === 'failed');
  const showExecute =
    permissions.can_execute && (status === 'approved' || (status === 'failed' && approvalValid));
  const showReconcile = permissions.can_reconcile && status === 'uncertain';

  const blockedReason = dirty
    ? 'Save or discard your edits first — approvals bind to the saved payload.'
    : action.validation_errors.length > 0
      ? 'Resolve the validation errors before approving.'
      : null;

  const rejectReason = reason.trim();
  const rejectError =
    rejectTouched && !rejectReason
      ? 'A reason is required'
      : rejectReason.length > LIMITS.rejectReason
        ? `At most ${LIMITS.rejectReason} characters`
        : null;

  const submitApprove = () => {
    // Send exactly the version + payload hash the reviewer is looking at.
    approve.mutate(
      { version: action.version, payloadHash: action.payload_hash, comment },
      {
        onSuccess: (detail) => {
          approveModal.close();
          setComment('');
          notifySuccess('Approved', `Version ${detail.version} is approved and ready to execute.`);
        },
        onError: (error) => {
          approveModal.close();
          handleError('Approval failed', error, onConflict);
        },
      },
    );
  };

  const submitReject = () => {
    setRejectTouched(true);
    if (!rejectReason || rejectReason.length > LIMITS.rejectReason) return;
    reject.mutate(rejectReason, {
      onSuccess: () => {
        rejectModal.close();
        setReason('');
        setRejectTouched(false);
        notifySuccess('Rejected', 'The proposal was rejected and will not be executed.');
      },
      onError: (error) => {
        rejectModal.close();
        handleError('Rejection failed', error, onConflict);
      },
    });
  };

  const submitCancel = () => {
    cancel.mutate(cancelReason, {
      onSuccess: () => {
        cancelModal.close();
        setCancelReason('');
        notifySuccess('Cancelled', 'The proposal was cancelled.');
      },
      onError: (error) => {
        cancelModal.close();
        handleError('Cancel failed', error, onConflict);
      },
    });
  };

  const submitExecute = () => {
    execute.mutate(
      { version: action.version, payloadHash: action.payload_hash },
      {
        onSuccess: (accepted) => {
          executeModal.close();
          onQueued(accepted.action.updated_at);
          notifySuccess(
            'Execution queued',
            'The approved payload is being written. Status updates automatically.',
          );
        },
        onError: (error) => {
          executeModal.close();
          handleError('Execution failed to start', error, onConflict);
        },
      },
    );
  };

  const submitReconcile = () => {
    reconcile.mutate(undefined, {
      onSuccess: (accepted) => {
        onQueued(accepted.action.updated_at);
        notifySuccess(
          'Reconciliation queued',
          'NUVEXA is looking the external item up. Status updates automatically.',
        );
      },
      onError: (error) => handleError('Reconcile failed', error, onConflict),
    });
  };

  const nothingToShow =
    !editable && !showApprove && !showReject && !showCancel && !showExecute && !showReconcile;
  if (nothingToShow) {
    return (
      <Text size="xs" c="dimmed">
        {action.permissions ? 'No actions available for you in this state.' : 'Read-only.'}
      </Text>
    );
  }

  return (
    <>
      <Group gap="sm" wrap="wrap">
        {editable ? (
          <>
            <Button
              leftSection={<IconDeviceFloppy size={16} aria-hidden />}
              onClick={onSave}
              loading={saving}
              disabled={!dirty || busy}
              variant={dirty ? 'filled' : 'default'}
            >
              Save changes
            </Button>
            {dirty ? (
              <Button variant="subtle" color="gray" onClick={onDiscard} disabled={saving}>
                Discard
              </Button>
            ) : null}
          </>
        ) : null}
        {showApprove ? (
          <Tooltip label={blockedReason ?? ''} disabled={!blockedReason}>
            <Button
              color="blue"
              leftSection={<IconCheck size={16} aria-hidden />}
              onClick={approveModal.open}
              loading={approve.isPending}
              disabled={Boolean(blockedReason) || busy || saving}
              data-testid="approve-button"
            >
              Approve
            </Button>
          </Tooltip>
        ) : null}
        {showExecute ? (
          <Button
            color="green"
            leftSection={
              status === 'failed' ? (
                <IconRotate size={16} aria-hidden />
              ) : (
                <IconPlayerPlay size={16} aria-hidden />
              )
            }
            onClick={executeModal.open}
            loading={execute.isPending}
            disabled={dirty || busy || saving}
            data-testid="execute-button"
          >
            {status === 'failed' ? 'Retry execution' : 'Execute'}
          </Button>
        ) : null}
        {showReconcile ? (
          <Button
            color="grape"
            leftSection={<IconRefreshDot size={16} aria-hidden />}
            onClick={submitReconcile}
            loading={reconcile.isPending}
            disabled={busy}
          >
            Reconcile
          </Button>
        ) : null}
        {showReject ? (
          <Button
            color="red"
            variant="outline"
            leftSection={<IconX size={16} aria-hidden />}
            onClick={rejectModal.open}
            loading={reject.isPending}
            disabled={busy || saving}
          >
            Reject
          </Button>
        ) : null}
        {showCancel ? (
          <Button
            color="gray"
            variant="subtle"
            leftSection={<IconBan size={16} aria-hidden />}
            onClick={cancelModal.open}
            loading={cancel.isPending}
            disabled={busy || saving}
          >
            Cancel proposal
          </Button>
        ) : null}
      </Group>
      {queued ? (
        <Group gap={6} mt={6} role="status">
          <Loader size={12} color="cyan" aria-hidden />
          <Text size="xs" c="dimmed">
            Queued — waiting for a worker to pick up the job. Status updates automatically.
          </Text>
        </Group>
      ) : null}
      {showApprove && blockedReason ? (
        <Text size="xs" c="dimmed" mt={6}>
          {blockedReason}
        </Text>
      ) : null}

      <Modal
        opened={approveOpen}
        onClose={approve.isPending ? () => undefined : approveModal.close}
        title="Approve this exact payload?"
      >
        <Stack gap="md">
          <Text size="sm">
            You are approving <b>{actionTypeLabel(action.action_type)}</b> “{action.title}”. The approval is
            bound to this version and payload hash — any later edit invalidates it.
          </Text>
          <HashLine action={action} />
          <Textarea
            label="Comment (optional)"
            value={comment}
            onChange={(event) => setComment(event.currentTarget.value)}
            maxLength={LIMITS.approveComment}
            autosize
            minRows={2}
            description={`${comment.length}/${LIMITS.approveComment}`}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={approveModal.close} disabled={approve.isPending}>
              Back
            </Button>
            <Button
              color="blue"
              onClick={submitApprove}
              loading={approve.isPending}
              data-testid="confirm-approve"
            >
              Confirm approval
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={rejectOpen}
        onClose={reject.isPending ? () => undefined : rejectModal.close}
        title="Reject this proposal"
      >
        <Stack gap="md">
          <Text size="sm">The proposal will not be executed. Rejections are recorded in the audit log.</Text>
          <Textarea
            label="Reason"
            required
            withAsterisk
            value={reason}
            onChange={(event) => setReason(event.currentTarget.value)}
            onBlur={() => setRejectTouched(true)}
            error={rejectError}
            autosize
            minRows={3}
            maxLength={LIMITS.rejectReason}
            description={`${reason.length}/${LIMITS.rejectReason}`}
            data-autofocus
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={rejectModal.close} disabled={reject.isPending}>
              Back
            </Button>
            <Button color="red" onClick={submitReject} loading={reject.isPending} disabled={!rejectReason}>
              Reject proposal
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={cancelOpen}
        onClose={cancel.isPending ? () => undefined : cancelModal.close}
        title="Cancel this proposal?"
      >
        <Stack gap="md">
          <Text size="sm">
            Cancelled proposals are kept for audit but can no longer be approved or executed.
          </Text>
          <Textarea
            label="Reason (optional)"
            value={cancelReason}
            onChange={(event) => setCancelReason(event.currentTarget.value)}
            maxLength={LIMITS.cancelReason}
            autosize
            minRows={2}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={cancelModal.close} disabled={cancel.isPending}>
              Back
            </Button>
            <Button color="gray" onClick={submitCancel} loading={cancel.isPending}>
              Cancel proposal
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={executeOpen}
        onClose={execute.isPending ? () => undefined : executeModal.close}
        title={status === 'failed' ? 'Retry execution?' : 'Execute approved action?'}
      >
        <Stack gap="md">
          {action.target_integration === 'azure_devops' ? (
            <Alert color="blue" variant="light">
              This creates a work item in Azure DevOps. NUVEXA checks for an existing item with the same
              idempotency tag first, so a retry never creates a duplicate it can detect.
            </Alert>
          ) : action.target_integration === 'reply_drafts' ? (
            <Alert color="blue" variant="light">
              This stores a reply draft in NUVEXA. Drafts are never sent by NUVEXA.
            </Alert>
          ) : null}
          <HashLine action={action} />
          <Group justify="flex-end">
            <Button variant="default" onClick={executeModal.close} disabled={execute.isPending}>
              Back
            </Button>
            <Button
              color="green"
              onClick={submitExecute}
              loading={execute.isPending}
              data-testid="confirm-execute"
            >
              {status === 'failed' ? 'Retry now' : 'Execute now'}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}
