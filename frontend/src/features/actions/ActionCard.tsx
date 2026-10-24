import {
  Accordion,
  Alert,
  Badge,
  Button,
  Card,
  Code,
  CopyButton,
  Divider,
  Group,
  List,
  Skeleton,
  Stack,
  Table,
  Text,
  ThemeIcon,
  Title,
  Tooltip,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconBrandAzure,
  IconCheck,
  IconCircleOff,
  IconCopy,
  IconFingerprint,
  IconInfoCircle,
  IconListCheck,
  IconMailForward,
  IconRefresh,
  IconShieldCheck,
} from '@tabler/icons-react';
import { useState } from 'react';

import type { ApiError } from '../../api/client';
import type { ActionDetail, ActionType, ApprovalOut, ExecutionOut } from '../../api/types';
import { ErrorState } from '../../components/ErrorState';
import { ExternalLink } from '../../components/ExternalLink';
import { PlainText } from '../../components/PlainText';
import { StatusBadge } from '../../components/StatusBadge';
import { TimeText } from '../../components/TimeText';
import { JsonBlock } from '../../components/JsonBlock';
import { TARGET_LABELS, actionTypeLabel, formatLatency, humanize, shortHash } from '../../lib/format';
import { ActionEditor } from './ActionEditors';
import { useActionDetail, useActionHistory, type QueuedWatch } from './hooks';

const TYPE_ICON: Record<ActionType, typeof IconBrandAzure> = {
  create_azure_devops_work_item: IconBrandAzure,
  propose_email_reply: IconMailForward,
  create_follow_up_task: IconListCheck,
  mark_no_action: IconCircleOff,
};

const EDITABLE_STATUSES = new Set(['pending_approval', 'approved']);
const WATCH_MS = 120_000;

interface SaveNotice {
  version: number;
  hash: string;
  invalidated: ApprovalOut | null;
}

function approvalIsCurrent(action: ActionDetail, approval: ApprovalOut | null): boolean {
  return (
    approval !== null &&
    !approval.invalidated_at &&
    approval.action_version === action.version &&
    approval.payload_hash === action.payload_hash
  );
}

export function CurrentApproval({ action }: { action: ActionDetail }) {
  const approval = action.current_approval;
  if (!approval) {
    return (
      <Text size="sm" c="dimmed">
        No current approval.
      </Text>
    );
  }
  const current = approvalIsCurrent(action, approval);
  const who = approval.decided_by_name ?? 'a reviewer';
  return (
    <Alert
      color={current ? (approval.decision === 'approved' ? 'blue' : 'red') : 'orange'}
      variant="light"
      icon={current ? <IconShieldCheck size={18} aria-hidden /> : <IconAlertTriangle size={18} aria-hidden />}
      p="sm"
      title={
        current
          ? `${approval.decision === 'approved' ? 'Approved' : 'Rejected'} by ${who}`
          : `Approval by ${who} is no longer valid`
      }
    >
      <Stack gap={4}>
        <Text size="xs">
          <TimeText value={approval.created_at} size="xs" /> · for version v{approval.action_version} ·
          payload <Code fz="xs">{shortHash(approval.payload_hash)}</Code>
        </Text>
        {approval.reason ? (
          <Text size="xs">
            Comment: <PlainText inline>{approval.reason}</PlainText>
          </Text>
        ) : null}
        {!current ? (
          <Text size="xs" fw={600}>
            {approval.invalidated_at
              ? `Invalidated ${approval.invalidated_reason ? `(${approval.invalidated_reason})` : ''} — the payload changed after approval.`
              : `It was given for v${approval.action_version}; the action is now v${action.version}.`}{' '}
            A new approval is required before execution.
          </Text>
        ) : null}
      </Stack>
    </Alert>
  );
}

function ApprovalHistory({ approvals }: { approvals: ApprovalOut[] }) {
  if (approvals.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No decisions yet.
      </Text>
    );
  }
  return (
    <Stack gap="xs">
      {approvals.map((approval) => (
        <Group key={approval.id} gap="xs" wrap="wrap" align="flex-start">
          <Badge
            color={approval.decision === 'approved' ? 'blue' : 'red'}
            variant={approval.invalidated_at ? 'outline' : 'light'}
          >
            {approval.decision === 'approved' ? 'Approved' : 'Rejected'}
          </Badge>
          <Stack gap={0} style={{ flex: 1, minWidth: 200 }}>
            <Text size="sm" className={approval.invalidated_at ? 'nuvexa-invalidated' : undefined}>
              {approval.decided_by_name ?? 'Reviewer'} · v{approval.action_version} ·{' '}
              <Code fz="xs">{shortHash(approval.payload_hash)}</Code> ·{' '}
              <TimeText value={approval.created_at} size="sm" />
            </Text>
            {approval.reason ? (
              <Text size="xs" c="dimmed">
                <PlainText inline>{approval.reason}</PlainText>
              </Text>
            ) : null}
            {approval.invalidated_at ? (
              <Text size="xs" c="orange.7" fw={600}>
                Invalidated <TimeText value={approval.invalidated_at} size="xs" />
                {approval.invalidated_reason ? ` — ${approval.invalidated_reason}` : ''}
              </Text>
            ) : null}
          </Stack>
        </Group>
      ))}
    </Stack>
  );
}

export function ExecutionResults({ executions }: { executions: ExecutionOut[] }) {
  if (executions.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        Not executed yet.
      </Text>
    );
  }
  return (
    <Table.ScrollContainer minWidth={720}>
      <Table aria-label="Execution attempts" verticalSpacing="xs">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Attempt</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>External item</Table.Th>
            <Table.Th>Retries</Table.Th>
            <Table.Th>Latency</Table.Th>
            <Table.Th>Started</Table.Th>
            <Table.Th>Error</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {executions.map((execution) => (
            <Table.Tr key={execution.id}>
              <Table.Td className="nuvexa-tabular">
                #{execution.attempt_number} · v{execution.action_version}
              </Table.Td>
              <Table.Td>
                <Group gap={4}>
                  <StatusBadge kind="execution" status={execution.status} size="sm" />
                  {execution.deduplicated ? (
                    <Tooltip label="An existing item with the same idempotency tag was found; nothing new was created.">
                      <Badge size="sm" variant="outline" color="teal">
                        Deduplicated
                      </Badge>
                    </Tooltip>
                  ) : null}
                </Group>
              </Table.Td>
              <Table.Td>
                {execution.external_id ? (
                  <Stack gap={0}>
                    <Text size="sm" ff="monospace">
                      #{execution.external_id}
                    </Text>
                    <ExternalLink url={execution.external_url}>Open work item</ExternalLink>
                  </Stack>
                ) : (
                  '—'
                )}
              </Table.Td>
              <Table.Td className="nuvexa-tabular">{execution.retry_count}</Table.Td>
              <Table.Td className="nuvexa-tabular">{formatLatency(execution.latency_ms)}</Table.Td>
              <Table.Td>
                <TimeText value={execution.started_at} size="sm" />
              </Table.Td>
              <Table.Td maw={260}>
                {execution.error_message || execution.error_category ? (
                  <Text size="xs" c="red.7" style={{ overflowWrap: 'anywhere' }}>
                    {execution.error_category ? `${humanize(execution.error_category)}: ` : ''}
                    {execution.error_message ?? ''}
                    {execution.provider_status_code ? ` (HTTP ${execution.provider_status_code})` : ''}
                  </Text>
                ) : (
                  '—'
                )}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

function ActionAuditHistory({ actionId }: { actionId: string }) {
  const history = useActionHistory(actionId);
  if (history.isPending) return <Skeleton height={60} />;
  if (history.isError)
    return <ErrorState error={history.error} compact onRetry={() => void history.refetch()} />;
  if (history.data.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No audit events.
      </Text>
    );
  }
  return (
    <Stack gap={6}>
      {history.data.map((event) => (
        <Group key={event.id} gap="xs" wrap="wrap">
          <StatusBadge kind="audit" status={event.outcome} size="xs" />
          <Text size="sm" fw={500}>
            {humanize(event.event_type)}
          </Text>
          <Text size="xs" c="dimmed">
            {event.actor_name ?? humanize(event.actor_type)} · <TimeText value={event.created_at} size="xs" />
          </Text>
        </Group>
      ))}
    </Stack>
  );
}

export function ActionCard({ actionId, focused = false }: { actionId: string; focused?: boolean }) {
  const [watch, setWatch] = useState<QueuedWatch | null>(null);
  const query = useActionDetail(actionId, { watch });
  const [conflict, setConflict] = useState<ApiError | null>(null);
  const [saveNotice, setSaveNotice] = useState<SaveNotice | null>(null);
  const [reloading, setReloading] = useState(false);

  if (query.isPending) {
    return (
      <Card>
        <Stack gap="sm" role="status" aria-busy="true" aria-label="Loading action">
          <Skeleton height={24} width="60%" />
          <Skeleton height={14} />
          <Skeleton height={180} />
        </Stack>
      </Card>
    );
  }
  if (query.isError) {
    return (
      <ErrorState error={query.error} onRetry={() => void query.refetch()} retrying={query.isFetching} />
    );
  }

  const action = query.data;
  const Icon = TYPE_ICON[action.action_type] ?? IconInfoCircle;
  const editable = Boolean(action.permissions?.can_edit) && EDITABLE_STATUSES.has(action.status);
  const superseded = action.status === 'superseded';
  // A queued execute/reconcile job has not been picked up while the action is unchanged.
  const queued = watch !== null && action.updated_at === watch.updatedAt && action.status !== 'executing';

  const reload = () => {
    setReloading(true);
    void query.refetch().finally(() => {
      setReloading(false);
      setConflict(null);
      setSaveNotice(null);
    });
  };

  return (
    <Card
      component="article"
      aria-labelledby={`action-${action.id}-title`}
      style={{
        borderColor: focused ? 'var(--mantine-primary-color-filled)' : undefined,
        borderWidth: focused ? 2 : undefined,
        opacity: superseded ? 0.72 : undefined,
      }}
      data-testid={`action-card-${action.id}`}
    >
      <Stack gap="md">
        <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
          <Group gap="sm" wrap="nowrap" align="flex-start" style={{ minWidth: 0, flex: '1 1 320px' }}>
            <ThemeIcon variant="light" size={38} radius="md" aria-hidden>
              <Icon size={20} />
            </ThemeIcon>
            <Stack gap={2} style={{ minWidth: 0 }}>
              <Text size="xs" c="dimmed" tt="uppercase" fw={600} lts={0.5}>
                {actionTypeLabel(action.action_type)} ·{' '}
                {TARGET_LABELS[action.target_integration] ?? action.target_integration}
              </Text>
              <Title order={3} id={`action-${action.id}-title`} style={{ overflowWrap: 'anywhere' }}>
                {action.title}
              </Title>
            </Stack>
          </Group>
          <Stack gap={6} align="flex-end">
            <StatusBadge
              kind="action"
              status={action.status}
              size="lg"
              withTooltip
              data-testid="action-status"
            />
            <Group gap={6} wrap="nowrap">
              <Tooltip label="Every edit creates a new version">
                <Badge variant="outline" color="gray" aria-label={`Version ${action.version}`}>
                  v{action.version}
                </Badge>
              </Tooltip>
              <CopyButton value={action.payload_hash}>
                {({ copied, copy }) => (
                  <Tooltip label={copied ? 'Copied' : `Payload SHA-256: ${action.payload_hash}`}>
                    <Badge
                      component="button"
                      type="button"
                      onClick={copy}
                      variant="outline"
                      color="gray"
                      leftSection={
                        copied ? (
                          <IconCheck size={12} aria-hidden />
                        ) : (
                          <IconFingerprint size={12} aria-hidden />
                        )
                      }
                      style={{ cursor: 'pointer', fontFamily: 'var(--mantine-font-family-monospace)' }}
                      aria-label={`Payload hash ${shortHash(action.payload_hash)}. Copy full hash`}
                      data-testid="payload-hash"
                    >
                      {shortHash(action.payload_hash)}
                    </Badge>
                  </Tooltip>
                )}
              </CopyButton>
            </Group>
          </Stack>
        </Group>

        {conflict ? (
          <Alert
            color="orange"
            variant="filled"
            role="alert"
            icon={<IconRefresh size={18} aria-hidden />}
            title="This action changed — reload before continuing"
          >
            <Stack gap="xs">
              <Text size="sm">
                Someone else edited, approved or executed this action after you loaded it, so your request was
                refused. Reload to review the latest version and payload.
              </Text>
              <Text size="xs" ff="monospace">
                ref {conflict.supportReference}
              </Text>
              <Button size="xs" color="dark" onClick={reload} loading={reloading} w="fit-content">
                Reload action
              </Button>
            </Stack>
          </Alert>
        ) : null}

        {saveNotice ? (
          <Alert
            color={saveNotice.invalidated ? 'orange' : 'blue'}
            variant="light"
            role="status"
            withCloseButton
            closeButtonLabel="Dismiss"
            onClose={() => setSaveNotice(null)}
            icon={
              saveNotice.invalidated ? (
                <IconAlertTriangle size={18} aria-hidden />
              ) : (
                <IconCheck size={18} aria-hidden />
              )
            }
            title={`Saved as version v${saveNotice.version}`}
            data-testid="save-notice"
          >
            <Text size="sm">
              New payload hash <Code fz="xs">{shortHash(saveNotice.hash)}</Code>.{' '}
              {saveNotice.invalidated
                ? `The previous approval by ${saveNotice.invalidated.decided_by_name ?? 'a reviewer'} (v${saveNotice.invalidated.action_version}) was invalidated — this version must be approved again before it can be executed.`
                : 'Approvals always bind to the saved version, so any earlier approval no longer applies.'}
            </Text>
          </Alert>
        ) : null}

        {superseded ? (
          <Alert color="gray" variant="light" icon={<IconInfoCircle size={18} aria-hidden />}>
            Superseded by a newer analysis of this email. It can no longer be approved or executed.
          </Alert>
        ) : null}

        <Stack gap={4}>
          <Text size="sm" fw={600}>
            Why this action
          </Text>
          <PlainText c="dimmed">{action.rationale || '—'}</PlainText>
          {action.expected_impact ? (
            <Text size="sm">
              <Text span fw={600}>
                Expected impact:{' '}
              </Text>
              <PlainText inline>{action.expected_impact}</PlainText>
            </Text>
          ) : null}
        </Stack>

        {action.validation_errors.length > 0 ? (
          <Alert
            color="red"
            variant="light"
            title="Validation errors"
            icon={<IconAlertTriangle size={18} aria-hidden />}
          >
            <List size="sm">
              {action.validation_errors.map((e) => (
                <List.Item key={e}>{e}</List.Item>
              ))}
            </List>
          </Alert>
        ) : null}
        {action.validation_warnings.length > 0 ? (
          <Alert
            color="yellow"
            variant="light"
            title="Warnings"
            icon={<IconInfoCircle size={18} aria-hidden />}
          >
            <List size="sm">
              {action.validation_warnings.map((w) => (
                <List.Item key={w}>{w}</List.Item>
              ))}
            </List>
          </Alert>
        ) : null}
        {action.last_error ? (
          <Alert
            color="red"
            variant="light"
            title="Last error"
            icon={<IconAlertTriangle size={18} aria-hidden />}
          >
            <Text size="sm" style={{ overflowWrap: 'anywhere' }}>
              {action.last_error}
            </Text>
          </Alert>
        ) : null}

        {action.external_id || action.external_url ? (
          <Group gap="xs">
            <Text size="sm" fw={600}>
              External item
            </Text>
            {action.external_id ? <Code>#{action.external_id}</Code> : null}
            <ExternalLink url={action.external_url}>Open</ExternalLink>
          </Group>
        ) : null}

        <CurrentApproval action={action} />

        <Divider />

        <ActionEditor
          key={`${action.id}:${action.version}:${action.payload_hash}`}
          action={action}
          editable={editable && !conflict}
          onSaved={(previous, next) => {
            const invalidated =
              previous.current_approval && approvalIsCurrent(previous, previous.current_approval)
                ? previous.current_approval
                : null;
            setSaveNotice({ version: next.version, hash: next.payload_hash, invalidated });
          }}
          onConflict={setConflict}
          queued={queued}
          onQueued={(updatedAt) => setWatch({ updatedAt, until: Date.now() + WATCH_MS })}
        />

        <Accordion variant="separated" multiple chevronPosition="right">
          <Accordion.Item value="executions">
            <Accordion.Control>Execution results ({action.executions.length})</Accordion.Control>
            <Accordion.Panel>
              <ExecutionResults executions={action.executions} />
            </Accordion.Panel>
          </Accordion.Item>
          <Accordion.Item value="approvals">
            <Accordion.Control>Approval history ({action.approvals.length})</Accordion.Control>
            <Accordion.Panel>
              <ApprovalHistory approvals={action.approvals} />
            </Accordion.Panel>
          </Accordion.Item>
          <Accordion.Item value="audit">
            <Accordion.Control>Audit trail</Accordion.Control>
            <Accordion.Panel>
              <ActionAuditHistory actionId={action.id} />
            </Accordion.Panel>
          </Accordion.Item>
          <Accordion.Item value="raw">
            <Accordion.Control>Raw payload &amp; identifiers</Accordion.Control>
            <Accordion.Panel>
              <Stack gap="xs">
                <Text size="xs" c="dimmed">
                  Idempotency key <Code fz="xs">{action.idempotency_key}</Code> · source {action.source}
                </Text>
                <Group gap="xs">
                  <Text size="xs" c="dimmed">
                    Payload SHA-256
                  </Text>
                  <Code fz="xs" style={{ wordBreak: 'break-all' }}>
                    {action.payload_hash}
                  </Code>
                  <CopyButton value={action.payload_hash}>
                    {({ copied, copy }) => (
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        onClick={copy}
                        leftSection={<IconCopy size={12} aria-hidden />}
                      >
                        {copied ? 'Copied' : 'Copy'}
                      </Button>
                    )}
                  </CopyButton>
                </Group>
                <JsonBlock value={action.payload} />
              </Stack>
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      </Stack>
    </Card>
  );
}
