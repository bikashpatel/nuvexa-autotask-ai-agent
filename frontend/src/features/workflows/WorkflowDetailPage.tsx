import {
  Alert,
  Anchor,
  Badge,
  Breadcrumbs,
  Button,
  Card,
  Collapse,
  Grid,
  Group,
  Skeleton,
  Stack,
  Stepper,
  Table,
  Text,
  Timeline,
  Title,
  UnstyledButton,
  useMatches,
} from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import {
  IconAlertTriangle,
  IconArrowsExchange,
  IconBan,
  IconBrain,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconClock,
  IconHandStop,
  IconPlayerPlay,
  IconPlayerTrackNext,
  IconPlug,
  IconRefresh,
  IconTool,
  IconX,
} from '@tabler/icons-react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router';

import { actionsApi, workflowsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import type { ActionOut, AuditEventOut, WorkflowEventOut, WorkflowEventType } from '../../api/types';
import { ConfirmModal } from '../../components/ConfirmModal';
import { ErrorState } from '../../components/ErrorState';
import { ExternalLink } from '../../components/ExternalLink';
import { JsonBlock } from '../../components/JsonBlock';
import { FactList } from '../../components/LabeledValue';
import { DetailSkeleton } from '../../components/LoadingSkeleton';
import { SectionCard } from '../../components/SectionCard';
import { StatusBadge } from '../../components/StatusBadge';
import { TimeText } from '../../components/TimeText';
import {
  actionTypeLabel,
  formatCurrency,
  formatDateTime,
  formatInteger,
  formatLatency,
  humanize,
} from '../../lib/format';
import { isOwnKey } from '../../lib/lookup';
import { notifyError, notifySuccess } from '../../lib/notify';
import {
  POLL_INTERVAL_MS,
  TERMINAL_WORKFLOW_STATUSES,
  isActionInFlight,
  isWorkflowInFlight,
} from '../../theme/status';
import { useHasPermission } from '../auth/session';
import { useReanalyzeWorkflow } from '../inbox/hooks';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { WORKFLOW_STEPS, computeStepperState, extractEventFacts } from './workflowSteps';

const EVENT_ICONS: Record<WorkflowEventType, typeof IconCheck> = {
  step_started: IconPlayerPlay,
  step_completed: IconCheck,
  step_failed: IconX,
  llm_call: IconBrain,
  tool_call: IconTool,
  interrupt: IconHandStop,
  resume: IconPlayerTrackNext,
  integration_call: IconPlug,
  status_changed: IconArrowsExchange,
};

function eventColor(event: WorkflowEventOut): string {
  const status = event.status.toLowerCase();
  if (event.event_type === 'step_failed' || /fail|error|denied/.test(status)) return 'red';
  if (/uncertain|ambiguous/.test(status)) return 'grape';
  if (/reject/.test(status)) return 'orange';
  if (/skip/.test(status)) return 'gray';
  if (/start|running|pending/.test(status)) return 'cyan';
  if (/ok|success|succeeded|completed|done/.test(status)) return 'green';
  if (event.event_type === 'interrupt') return 'orange';
  return 'gray';
}

function WorkflowStepper({
  detail,
}: {
  detail: { run: Parameters<typeof computeStepperState>[0]; events: WorkflowEventOut[] };
}) {
  const orientation = useMatches<'vertical' | 'horizontal'>({ base: 'vertical', lg: 'horizontal' });
  const state = computeStepperState(detail.run, detail.events);
  const terminalColor = state.terminal === 'cancelled' ? 'gray' : 'red';
  const terminalLabel = state.terminal ? humanize(state.terminal) : null;

  return (
    <Stepper
      active={state.active}
      orientation={orientation}
      size="sm"
      iconSize={30}
      allowNextStepsSelect={false}
      wrap
      aria-label="Workflow progress"
    >
      {WORKFLOW_STEPS.map((step, index) => {
        const stopped = state.stoppedIndex === index;
        return (
          <Stepper.Step
            key={step.status}
            label={stopped && terminalLabel ? `${step.label} — ${terminalLabel}` : step.label}
            description={
              stopped && terminalLabel ? `Stopped: ${terminalLabel.toLowerCase()}` : step.description
            }
            loading={state.loadingIndex === index}
            color={stopped ? terminalColor : undefined}
            icon={
              stopped ? (
                state.terminal === 'cancelled' ? (
                  <IconBan size={16} aria-hidden />
                ) : (
                  <IconX size={16} aria-hidden />
                )
              ) : state.active === index && !state.loadingIndex ? (
                <IconClock size={16} aria-hidden />
              ) : undefined
            }
            allowStepSelect={false}
            aria-current={state.active === index ? 'step' : undefined}
          />
        );
      })}
    </Stepper>
  );
}

function EventItem({ event }: { event: WorkflowEventOut }) {
  const [open, { toggle }] = useDisclosure(false);
  const facts = extractEventFacts(event.details ?? {});
  const Icon = isOwnKey(EVENT_ICONS, event.event_type) ? EVENT_ICONS[event.event_type] : IconArrowsExchange;
  const color = eventColor(event);
  const hasDetails = Object.keys(event.details ?? {}).length > 0;
  return (
    <Timeline.Item
      color={color}
      bullet={<Icon size={12} aria-hidden />}
      title={
        <Group gap="xs" wrap="wrap">
          <Text size="sm" fw={600}>
            {humanize(event.step)}
          </Text>
          <Badge size="xs" variant="outline" color="gray">
            {humanize(event.event_type)}
          </Badge>
          <Badge size="xs" variant="light" color={color}>
            {event.status}
          </Badge>
          {event.latency_ms !== null ? (
            <Text size="xs" c="dimmed" className="nuvexa-tabular">
              {formatLatency(event.latency_ms)}
            </Text>
          ) : null}
        </Group>
      }
    >
      <Stack gap={4}>
        {event.message ? (
          <Text size="sm" c="dimmed" style={{ overflowWrap: 'anywhere' }}>
            {event.message}
          </Text>
        ) : null}
        <Group gap="md" wrap="wrap">
          {facts.model ? (
            <Text size="xs">
              Model <b>{[facts.provider, facts.model].filter(Boolean).join(' / ')}</b>
            </Text>
          ) : null}
          {facts.totalTokens !== null ? (
            <Text size="xs" className="nuvexa-tabular">
              Tokens <b>{formatInteger(facts.totalTokens)}</b>
              {facts.inputTokens !== null || facts.outputTokens !== null
                ? ` (${formatInteger(facts.inputTokens ?? 0)} in / ${formatInteger(facts.outputTokens ?? 0)} out)`
                : ''}
            </Text>
          ) : null}
          {facts.costUsd !== null ? (
            <Text size="xs">
              Est. cost <b>{formatCurrency(facts.costUsd)}</b>
            </Text>
          ) : null}
          {facts.toolName ? (
            <Text size="xs">
              Tool <b>{facts.toolName}</b>
            </Text>
          ) : null}
          {facts.attempts !== null ? (
            <Text size="xs" className="nuvexa-tabular">
              Attempts <b>{facts.attempts}</b>
            </Text>
          ) : null}
          {facts.retryCount !== null ? (
            <Text size="xs" className="nuvexa-tabular">
              Retries <b>{facts.retryCount}</b>
            </Text>
          ) : null}
          <TimeText value={event.created_at} size="xs" c="dimmed" mode="absolute" />
        </Group>
        {facts.error || facts.errorCategory ? (
          <Text size="xs" c="red.7" style={{ overflowWrap: 'anywhere' }}>
            {facts.errorCategory ? `${humanize(facts.errorCategory)}: ` : ''}
            {facts.error ?? ''}
          </Text>
        ) : null}
        {facts.toolArgs ? (
          <>
            <Text size="xs" c="dimmed">
              Sanitized arguments
            </Text>
            <JsonBlock value={facts.toolArgs} maxHeight={200} />
          </>
        ) : null}
        {hasDetails ? (
          <>
            <UnstyledButton onClick={toggle} aria-expanded={open} w="fit-content">
              <Group gap={4}>
                {open ? (
                  <IconChevronDown size={14} aria-hidden />
                ) : (
                  <IconChevronRight size={14} aria-hidden />
                )}
                <Text size="xs" c="dimmed">
                  {open ? 'Hide' : 'Show'} details
                </Text>
              </Group>
            </UnstyledButton>
            <Collapse expanded={open}>
              <JsonBlock value={event.details} maxHeight={260} />
            </Collapse>
          </>
        ) : null}
      </Stack>
    </Timeline.Item>
  );
}

function ActionsTable({ actions }: { actions: ActionOut[] }) {
  if (actions.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No actions proposed (yet).
      </Text>
    );
  }
  return (
    <Table.ScrollContainer minWidth={640}>
      <Table aria-label="Proposed actions">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Action</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>Version</Table.Th>
            <Table.Th>External</Table.Th>
            <Table.Th>Created</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {actions.map((action) => (
            <Table.Tr key={action.id}>
              <Table.Td maw={320}>
                <Text size="xs" c="dimmed">
                  {actionTypeLabel(action.action_type)}
                </Text>
                <Anchor component={Link} to={`/actions/${action.id}`} size="sm" fw={600} lineClamp={2}>
                  {action.title}
                </Anchor>
              </Table.Td>
              <Table.Td>
                <StatusBadge kind="action" status={action.status} size="sm" />
              </Table.Td>
              <Table.Td className="nuvexa-tabular">v{action.version}</Table.Td>
              <Table.Td>
                {action.external_url ? (
                  <ExternalLink url={action.external_url}>#{action.external_id ?? 'open'}</ExternalLink>
                ) : (
                  '—'
                )}
              </Table.Td>
              <Table.Td>
                <TimeText value={action.created_at} size="sm" />
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

function AuditTrail({ actions }: { actions: ActionOut[] }) {
  const results = useQueries({
    queries: actions.map((action) => ({
      queryKey: queryKeys.actionHistory(action.id),
      queryFn: () => actionsApi.history(action.id),
    })),
  });
  if (actions.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No actions, so no action audit events.
      </Text>
    );
  }
  if (results.some((r) => r.isPending)) return <Skeleton height={80} />;
  const failed = results.find((r) => r.isError);
  if (failed?.error)
    return (
      <ErrorState error={failed.error} compact onRetry={() => results.forEach((r) => void r.refetch())} />
    );
  const titles = new Map(actions.map((a) => [a.id, a.title]));
  const events: AuditEventOut[] = results
    .flatMap((r) => r.data ?? [])
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  if (events.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No audit events yet.
      </Text>
    );
  }
  return (
    <Table.ScrollContainer minWidth={640}>
      <Table aria-label="Audit trail for this run's actions" verticalSpacing="xs">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>When</Table.Th>
            <Table.Th>Event</Table.Th>
            <Table.Th>Actor</Table.Th>
            <Table.Th>Action</Table.Th>
            <Table.Th>Outcome</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {events.map((event) => (
            <Table.Tr key={event.id}>
              <Table.Td>
                <TimeText value={event.created_at} size="sm" />
              </Table.Td>
              <Table.Td>{humanize(event.event_type)}</Table.Td>
              <Table.Td>{event.actor_name ?? humanize(event.actor_type)}</Table.Td>
              <Table.Td maw={220}>
                <Text size="sm" truncate>
                  {event.action_id ? (titles.get(event.action_id) ?? event.action_id) : '—'}
                </Text>
              </Table.Td>
              <Table.Td>
                <StatusBadge kind="audit" status={event.outcome} size="sm" />
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

function metricValue(value: unknown): string {
  if (typeof value === 'number') return Number.isInteger(value) ? formatInteger(value) : value.toFixed(4);
  if (typeof value === 'string') return value;
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (value === null || value === undefined) return '—';
  return JSON.stringify(value);
}

export function WorkflowDetailPage() {
  const { workflowId = '' } = useParams();
  const canRun = useHasPermission('workflow:run');
  const reanalyze = useReanalyzeWorkflow();
  const queryClient = useQueryClient();
  const [reanalyzeOpen, reanalyzeModal] = useDisclosure(false);
  const [cancelOpen, cancelModal] = useDisclosure(false);

  const workflow = useQuery({
    queryKey: queryKeys.workflow(workflowId),
    queryFn: () => workflowsApi.get(workflowId),
    enabled: Boolean(workflowId),
    refetchInterval: (query) => {
      const data = query.state.data;
      const busy =
        isWorkflowInFlight(data?.run.status) || data?.actions.some((a) => isActionInFlight(a.status));
      return busy ? POLL_INTERVAL_MS : false;
    },
  });

  const cancel = useMutation({
    mutationFn: () => workflowsApi.cancel(workflowId),
    onSuccess: () => {
      cancelModal.close();
      notifySuccess('Workflow cancelled', 'Open proposals were cancelled.');
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflow(workflowId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.actions });
    },
    onError: (error) => {
      cancelModal.close();
      notifyError('Could not cancel the workflow', error);
    },
  });

  if (workflow.isPending) return <DetailSkeleton />;
  if (workflow.isError) {
    return (
      <ErrorState
        error={workflow.error}
        onRetry={() => void workflow.refetch()}
        retrying={workflow.isFetching}
      />
    );
  }

  const { run, analysis, actions, events } = workflow.data;
  const terminal = TERMINAL_WORKFLOW_STATUSES.has(run.status);
  const inFlight = isWorkflowInFlight(run.status);
  const metrics = Object.entries(run.metrics ?? {});

  return (
    <Stack gap="md">
      <Breadcrumbs>
        <Anchor component={Link} to="/workflows" size="sm">
          Workflows
        </Anchor>
        <Text size="sm" c="dimmed" truncate maw={360}>
          {run.email_subject ?? run.id}
        </Text>
      </Breadcrumbs>
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
        <Stack gap={6} style={{ minWidth: 0, flex: '1 1 420px' }}>
          <Text size="xs" tt="uppercase" fw={600} c="dimmed" lts={0.6}>
            Workflow run · attempt #{run.analysis_attempt} · {humanize(run.trigger)} trigger
          </Text>
          <Title order={1} style={{ overflowWrap: 'anywhere' }}>
            {run.email_subject ?? 'Workflow run'}
          </Title>
          <Group gap="xs">
            <StatusBadge kind="workflow" status={run.status} size="lg" />
            {analysis && analysis.injection_flags.length > 0 ? (
              <Badge color="red" variant="filled" leftSection={<IconAlertTriangle size={12} aria-hidden />}>
                Injection indicators: {analysis.injection_flags.length}
              </Badge>
            ) : null}
          </Group>
        </Stack>
        <Group gap="sm">
          <Button component={Link} to={`/emails/${run.email_message_id}`} variant="default">
            Open email
          </Button>
          {canRun && !terminal && !inFlight ? (
            <Button
              variant="light"
              leftSection={<IconRefresh size={16} aria-hidden />}
              onClick={reanalyzeModal.open}
            >
              Request re-analysis
            </Button>
          ) : null}
          {canRun && !terminal ? (
            <Button
              variant="subtle"
              color="gray"
              leftSection={<IconBan size={16} aria-hidden />}
              onClick={cancelModal.open}
            >
              Cancel run
            </Button>
          ) : null}
        </Group>
      </Group>

      {run.error_message || run.error_category ? (
        <Alert
          color="red"
          variant="light"
          title={`Run failed${run.error_category ? `: ${humanize(run.error_category)}` : ''}`}
          icon={<IconAlertTriangle size={18} aria-hidden />}
        >
          <Text size="sm" style={{ overflowWrap: 'anywhere' }}>
            {run.error_message ?? 'No details.'}
          </Text>
        </Alert>
      ) : null}

      <Card padding="lg">
        <WorkflowStepper detail={{ run, events }} />
      </Card>

      <Grid gap="md" align="flex-start">
        <Grid.Col span={{ base: 12, lg: 7 }}>
          <SectionCard
            title="Event timeline"
            id="timeline"
            description="Step-by-step trace: model calls, proposal tool calls (sanitized arguments), integration calls and status changes."
          >
            {events.length === 0 ? (
              <Text size="sm" c="dimmed">
                No events recorded yet.
              </Text>
            ) : (
              <Timeline bulletSize={24} lineWidth={2} active={events.length}>
                {events.map((event) => (
                  <EventItem key={event.id} event={event} />
                ))}
              </Timeline>
            )}
          </SectionCard>
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 5 }}>
          <Stack gap="md">
            <SectionCard title="Run" id="run-facts">
              <FactList
                labelWidth={140}
                items={[
                  { label: 'Current step', value: humanize(run.current_step) },
                  {
                    label: 'Model',
                    value: [run.model_provider, run.model_name].filter(Boolean).join(' / ') || '—',
                  },
                  { label: 'Prompt version', value: run.prompt_version ?? '—' },
                  { label: 'Schema version', value: run.schema_version ?? '—' },
                  { label: 'Started', value: formatDateTime(run.started_at) },
                  { label: 'Completed', value: formatDateTime(run.completed_at) },
                  {
                    label: 'Awaiting review since',
                    value: <TimeText value={run.awaiting_review_since} size="sm" />,
                    hidden: !run.awaiting_review_since,
                  },
                  {
                    label: 'Correlation id',
                    value: (
                      <Text size="xs" ff="monospace" style={{ overflowWrap: 'anywhere' }}>
                        {run.correlation_id ?? '—'}
                      </Text>
                    ),
                  },
                ]}
              />
            </SectionCard>
            <SectionCard title="Metrics" id="metrics">
              {metrics.length === 0 ? (
                <Text size="sm" c="dimmed">
                  No metrics recorded.
                </Text>
              ) : (
                <FactList
                  labelWidth={180}
                  items={metrics.map(([key, value]) => ({
                    label: humanize(key),
                    value: (
                      <Text size="sm" className="nuvexa-tabular">
                        {metricValue(value)}
                      </Text>
                    ),
                  }))}
                />
              )}
            </SectionCard>
          </Stack>
        </Grid.Col>
      </Grid>

      <SectionCard title="Actions" id="actions">
        <ActionsTable actions={actions} />
      </SectionCard>
      <SectionCard title="Audit trail" id="audit-trail" description="Audit events for this run's actions.">
        <AuditTrail actions={actions} />
      </SectionCard>

      <ConfirmModal
        opened={reanalyzeOpen}
        onClose={reanalyzeModal.close}
        title="Request re-analysis?"
        confirmLabel="Re-analyze"
        loading={reanalyze.isPending}
        onConfirm={() => reanalyze.mutate(run.id, { onSuccess: () => reanalyzeModal.close() })}
      >
        Open proposals of this run are superseded and the email is analyzed again.
      </ConfirmModal>
      <ConfirmModal
        opened={cancelOpen}
        onClose={cancelModal.close}
        title="Cancel this workflow run?"
        confirmLabel="Cancel run"
        confirmColor="red"
        loading={cancel.isPending}
        onConfirm={() => cancel.mutate()}
      >
        All open proposals of this run are cancelled. Completed actions are not affected.
      </ConfirmModal>
    </Stack>
  );
}
