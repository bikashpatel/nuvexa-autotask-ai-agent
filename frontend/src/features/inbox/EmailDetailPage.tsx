import {
  Alert,
  Anchor,
  Badge,
  Breadcrumbs,
  Button,
  Grid,
  Group,
  Stack,
  Table,
  Text,
  Title,
} from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import {
  IconAlertTriangle,
  IconArrowRight,
  IconPaperclip,
  IconRefresh,
  IconSparkles,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router';

import { emailsApi, workflowsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import type { EmailDetail } from '../../api/types';
import { ConfidenceIndicator } from '../../components/ConfidenceIndicator';
import { ConfirmModal } from '../../components/ConfirmModal';
import { CategoryBadge, PriorityBadge } from '../../components/DomainBadges';
import { ErrorState } from '../../components/ErrorState';
import { FactList } from '../../components/LabeledValue';
import { DetailSkeleton } from '../../components/LoadingSkeleton';
import { SectionCard } from '../../components/SectionCard';
import { StatusBadge } from '../../components/StatusBadge';
import { TimeText } from '../../components/TimeText';
import { actionTypeLabel, formatBytes, humanize } from '../../lib/format';
import { POLL_INTERVAL_MS, TERMINAL_WORKFLOW_STATUSES, isWorkflowInFlight } from '../../theme/status';
import { useHasPermission } from '../auth/session';
import { EmailBody, EmailHeaders } from './EmailParts';
import { useAnalyzeEmail, useReanalyzeWorkflow } from './hooks';

function LatestWorkflow({ email }: { email: EmailDetail }) {
  const runId = email.latest_workflow_run_id;
  const canRun = useHasPermission('workflow:run');
  const analyze = useAnalyzeEmail();
  const reanalyze = useReanalyzeWorkflow();
  const [confirmOpen, confirm] = useDisclosure(false);

  const workflow = useQuery({
    queryKey: queryKeys.workflow(runId ?? 'none'),
    queryFn: () => workflowsApi.get(runId!),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const data = query.state.data;
      return isWorkflowInFlight(data?.run.status) ? POLL_INTERVAL_MS : false;
    },
  });

  if (!runId) {
    return (
      <SectionCard title="Analysis" id="analysis">
        <Stack gap="sm">
          <Text size="sm" c="dimmed">
            This email has not been analyzed yet.
          </Text>
          {canRun ? (
            <Button
              leftSection={<IconSparkles size={16} aria-hidden />}
              onClick={() => analyze.mutate(email.id)}
              loading={analyze.isPending}
              w="fit-content"
            >
              Analyze
            </Button>
          ) : null}
        </Stack>
      </SectionCard>
    );
  }

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

  const { run, analysis, actions } = workflow.data;
  const inFlight = isWorkflowInFlight(run.status);
  const terminal = TERMINAL_WORKFLOW_STATUSES.has(run.status);
  const openActions = actions.filter((a) => a.status !== 'superseded');

  return (
    <SectionCard
      title="Latest analysis"
      id="analysis"
      actions={<StatusBadge kind="workflow" status={run.status} />}
    >
      <Stack gap="md">
        <FactList
          items={[
            { label: 'Attempt', value: `#${run.analysis_attempt}` },
            { label: 'Current step', value: humanize(run.current_step) },
            {
              label: 'Model',
              value: run.model_name ? `${run.model_provider ?? ''} ${run.model_name}`.trim() : '—',
            },
            { label: 'Updated', value: <TimeText value={run.updated_at} size="sm" /> },
            { label: 'Error', value: run.error_message ?? '—', hidden: !run.error_message },
          ]}
          labelWidth={110}
        />
        {analysis ? (
          <Stack gap="xs">
            <Group gap="xs">
              <CategoryBadge category={analysis.category} />
              <PriorityBadge priority={analysis.priority} />
            </Group>
            <ConfidenceIndicator
              score={analysis.confidence_score}
              bucket={analysis.confidence_bucket}
              method={analysis.confidence_method}
            />
            {analysis.injection_flags.length > 0 ? (
              <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} aria-hidden />} p="xs">
                <Text size="sm" fw={600}>
                  Prompt-injection indicators detected ({analysis.injection_flags.length})
                </Text>
              </Alert>
            ) : null}
          </Stack>
        ) : null}
        {openActions.length > 0 ? (
          <Stack gap={6}>
            <Text size="sm" fw={600}>
              Proposed actions
            </Text>
            {openActions.map((action) => (
              <Group key={action.id} justify="space-between" wrap="nowrap" gap="xs">
                <Anchor component={Link} to={`/actions/${action.id}`} size="sm" truncate>
                  {actionTypeLabel(action.action_type)} — {action.title}
                </Anchor>
                <StatusBadge kind="action" status={action.status} size="sm" />
              </Group>
            ))}
          </Stack>
        ) : null}
        <Group gap="sm">
          <Button
            component={Link}
            to={openActions[0] ? `/actions/${openActions[0].id}` : `/workflows/${run.id}`}
            variant="light"
            rightSection={<IconArrowRight size={16} aria-hidden />}
          >
            {openActions[0] ? 'Review actions' : 'Open workflow'}
          </Button>
          <Button component={Link} to={`/workflows/${run.id}`} variant="subtle">
            Workflow timeline
          </Button>
        </Group>
        {canRun ? (
          inFlight ? (
            <Text size="xs" c="dimmed" role="status">
              Analysis in progress — this panel refreshes automatically.
            </Text>
          ) : terminal ? (
            <Button
              variant="default"
              leftSection={<IconSparkles size={16} aria-hidden />}
              onClick={() => analyze.mutate(email.id)}
              loading={analyze.isPending}
              w="fit-content"
            >
              Analyze again
            </Button>
          ) : (
            <>
              <Button
                variant="default"
                leftSection={<IconRefresh size={16} aria-hidden />}
                onClick={confirm.open}
                w="fit-content"
              >
                Re-analyze
              </Button>
              <ConfirmModal
                opened={confirmOpen}
                onClose={confirm.close}
                title="Re-analyze this email?"
                confirmLabel="Re-analyze"
                loading={reanalyze.isPending}
                onConfirm={() =>
                  reanalyze.mutate(run.id, {
                    onSuccess: () => {
                      confirm.close();
                    },
                  })
                }
              >
                The workflow restarts from normalization. All open proposals of this run (including approved
                but not executed ones) are superseded and must be reviewed again.
              </ConfirmModal>
            </>
          )
        ) : null}
      </Stack>
    </SectionCard>
  );
}

export function EmailDetailPage() {
  const { emailId = '' } = useParams();
  const email = useQuery({
    queryKey: queryKeys.email(emailId),
    queryFn: () => emailsApi.get(emailId),
    enabled: Boolean(emailId),
  });

  if (email.isPending) return <DetailSkeleton />;
  if (email.isError) {
    return (
      <ErrorState error={email.error} onRetry={() => void email.refetch()} retrying={email.isFetching} />
    );
  }
  const data = email.data;

  return (
    <Stack gap="md">
      <Breadcrumbs>
        <Anchor component={Link} to="/inbox" size="sm">
          Inbox
        </Anchor>
        <Text size="sm" c="dimmed" truncate maw={360}>
          {data.subject || '(no subject)'}
        </Text>
      </Breadcrumbs>
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
        <Stack gap={6} style={{ minWidth: 0, flex: '1 1 400px' }}>
          <Title order={1} style={{ overflowWrap: 'anywhere' }}>
            {data.subject || '(no subject)'}
          </Title>
          <Group gap="xs">
            <StatusBadge kind="email" status={data.analysis_status} />
            <CategoryBadge category={data.category} />
            <PriorityBadge priority={data.priority} />
            {data.has_attachments ? (
              <Badge variant="outline" color="gray" leftSection={<IconPaperclip size={12} aria-hidden />}>
                {data.attachments.length} attachment{data.attachments.length === 1 ? '' : 's'}
              </Badge>
            ) : null}
          </Group>
        </Stack>
      </Group>

      {data.content_changed_since_analysis ? (
        <Alert
          color="orange"
          variant="light"
          icon={<IconAlertTriangle size={18} aria-hidden />}
          title="Content changed since analysis"
        >
          This message was modified in the mailbox after it was analyzed. Re-analyze it before approving any
          proposal.
        </Alert>
      ) : null}

      <Grid gap="md">
        <Grid.Col span={{ base: 12, lg: 8 }}>
          <Stack gap="md">
            <SectionCard
              title="Message"
              id="message"
              description="Untrusted content — shown as plain text only."
            >
              <Stack gap="md">
                <EmailHeaders email={data} />
                <EmailBody email={data} />
              </Stack>
            </SectionCard>
            {data.attachments.length > 0 ? (
              <SectionCard
                title="Attachments"
                id="attachments"
                description="Metadata only. Attachment content is never downloaded or analyzed in this version."
              >
                <Table.ScrollContainer minWidth={480}>
                  <Table aria-label="Attachments">
                    <Table.Thead>
                      <Table.Tr>
                        <Table.Th>Name</Table.Th>
                        <Table.Th>Type</Table.Th>
                        <Table.Th>Size</Table.Th>
                        <Table.Th>Inline</Table.Th>
                      </Table.Tr>
                    </Table.Thead>
                    <Table.Tbody>
                      {data.attachments.map((att, i) => (
                        <Table.Tr key={`${att.name}-${i}`}>
                          <Table.Td style={{ overflowWrap: 'anywhere' }}>{att.name}</Table.Td>
                          <Table.Td>{att.content_type ?? '—'}</Table.Td>
                          <Table.Td className="nuvexa-tabular">{formatBytes(att.size)}</Table.Td>
                          <Table.Td>{att.is_inline ? 'Yes' : 'No'}</Table.Td>
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                </Table.ScrollContainer>
              </SectionCard>
            ) : null}
          </Stack>
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 4 }}>
          <div className="nuvexa-sticky-panel">
            <LatestWorkflow email={data} />
          </div>
        </Grid.Col>
      </Grid>
    </Stack>
  );
}
