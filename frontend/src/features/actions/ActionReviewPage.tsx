import {
  Alert,
  Anchor,
  Badge,
  Breadcrumbs,
  Button,
  Grid,
  Group,
  ScrollArea,
  Skeleton,
  Stack,
  Switch,
  Text,
  Title,
} from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import { IconAlertTriangle, IconLoader2, IconRefresh, IconRoute } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link, useParams } from 'react-router';

import { emailsApi, workflowsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import { ConfirmModal } from '../../components/ConfirmModal';
import { ErrorState } from '../../components/ErrorState';
import { InjectionWarning } from '../../components/InjectionWarning';
import { DetailSkeleton } from '../../components/LoadingSkeleton';
import { SectionCard } from '../../components/SectionCard';
import { StatusBadge } from '../../components/StatusBadge';
import { actionTypeLabel } from '../../lib/format';
import {
  POLL_INTERVAL_MS,
  TERMINAL_WORKFLOW_STATUSES,
  isActionInFlight,
  isWorkflowInFlight,
} from '../../theme/status';
import { useHasPermission } from '../auth/session';
import { EmailBody, EmailHeaders } from '../inbox/EmailParts';
import { useReanalyzeWorkflow } from '../inbox/hooks';
import { ActionCard } from './ActionCard';
import { AnalysisPanel } from './AnalysisPanel';
import { useActionDetail } from './hooks';

function OriginalEmail({ emailId }: { emailId: string }) {
  const email = useQuery({ queryKey: queryKeys.email(emailId), queryFn: () => emailsApi.get(emailId) });
  return (
    <SectionCard
      title="Original email"
      id="original-email"
      description="Untrusted content, shown as plain text."
      actions={
        <Anchor component={Link} to={`/emails/${emailId}`} size="sm">
          Open email
        </Anchor>
      }
    >
      {email.isPending ? (
        <Stack gap="xs" role="status" aria-busy="true" aria-label="Loading email">
          <Skeleton height={14} />
          <Skeleton height={14} width="70%" />
          <Skeleton height={220} />
        </Stack>
      ) : email.isError ? (
        <ErrorState error={email.error} compact onRetry={() => void email.refetch()} />
      ) : (
        <Stack gap="sm">
          <Text fw={650} style={{ overflowWrap: 'anywhere' }}>
            {email.data.subject || '(no subject)'}
          </Text>
          {email.data.content_changed_since_analysis ? (
            <Alert color="orange" variant="light" p="xs" icon={<IconAlertTriangle size={16} aria-hidden />}>
              <Text size="sm">Content changed since analysis — consider requesting re-analysis.</Text>
            </Alert>
          ) : null}
          <EmailHeaders email={email.data} />
          <ScrollArea.Autosize mah="60vh" type="auto" offsetScrollbars>
            <EmailBody email={email.data} />
          </ScrollArea.Autosize>
        </Stack>
      )}
    </SectionCard>
  );
}

export function ActionReviewPage() {
  const { actionId = '' } = useParams();
  const action = useActionDetail(actionId);
  const runId = action.data?.workflow_run_id;
  const canRun = useHasPermission('workflow:run');
  const reanalyze = useReanalyzeWorkflow();
  const [confirmOpen, confirm] = useDisclosure(false);
  const [showSuperseded, setShowSuperseded] = useState(false);

  const workflow = useQuery({
    queryKey: queryKeys.workflow(runId ?? 'none'),
    queryFn: () => workflowsApi.get(runId!),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const data = query.state.data;
      const busy =
        isWorkflowInFlight(data?.run.status) || data?.actions.some((a) => isActionInFlight(a.status));
      return busy ? POLL_INTERVAL_MS : false;
    },
  });

  if (action.isPending) return <DetailSkeleton />;
  if (action.isError) {
    return (
      <ErrorState error={action.error} onRetry={() => void action.refetch()} retrying={action.isFetching} />
    );
  }

  const current = action.data;
  const run = workflow.data?.run;
  const analysis = workflow.data?.analysis ?? null;
  const siblings = (workflow.data?.actions ?? []).filter((a) => a.id !== current.id);
  const visibleSiblings = siblings.filter((a) => showSuperseded || a.status !== 'superseded');
  const supersededCount = siblings.filter((a) => a.status === 'superseded').length;
  const canReanalyze =
    canRun && run && !isWorkflowInFlight(run.status) && !TERMINAL_WORKFLOW_STATUSES.has(run.status);

  return (
    <Stack gap="md">
      <Breadcrumbs>
        <Anchor component={Link} to="/approvals" size="sm">
          Approvals
        </Anchor>
        <Text size="sm" c="dimmed" truncate maw={360}>
          {current.title}
        </Text>
      </Breadcrumbs>

      <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
        <Stack gap={6} style={{ minWidth: 0, flex: '1 1 420px' }}>
          <Text size="xs" tt="uppercase" fw={600} c="dimmed" lts={0.6}>
            Action review · {actionTypeLabel(current.action_type)}
          </Text>
          <Title order={1} style={{ overflowWrap: 'anywhere' }}>
            {current.email_subject ?? current.title}
          </Title>
          <Group gap="xs">
            {run ? <StatusBadge kind="workflow" status={run.status} /> : null}
            {run ? (
              <Badge variant="outline" color="gray">
                Analysis attempt #{run.analysis_attempt}
              </Badge>
            ) : null}
          </Group>
        </Stack>
        <Group gap="sm">
          {runId ? (
            <Button
              component={Link}
              to={`/workflows/${runId}`}
              variant="default"
              leftSection={<IconRoute size={16} aria-hidden />}
            >
              Workflow timeline
            </Button>
          ) : null}
          {canReanalyze ? (
            <Button
              variant="light"
              leftSection={<IconRefresh size={16} aria-hidden />}
              onClick={confirm.open}
              loading={reanalyze.isPending}
            >
              Request re-analysis
            </Button>
          ) : null}
        </Group>
      </Group>

      {analysis ? <InjectionWarning flags={analysis.injection_flags} /> : null}

      {run && isWorkflowInFlight(run.status) ? (
        <Alert color="cyan" variant="light" icon={<IconLoader2 size={18} aria-hidden />} role="status">
          The workflow is{' '}
          {run.status === 'executing'
            ? 'executing an approved action'
            : `running (${run.current_step ?? run.status})`}
          . This page refreshes automatically.
        </Alert>
      ) : null}

      <Grid gap="md" align="flex-start">
        <Grid.Col span={{ base: 12, lg: 5 }}>
          <div className="nuvexa-sticky-panel">
            <OriginalEmail emailId={current.email_message_id} />
          </div>
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 7 }}>
          <Stack gap="md">
            {workflow.isPending && runId ? (
              <Skeleton height={280} />
            ) : workflow.isError ? (
              <ErrorState error={workflow.error} compact onRetry={() => void workflow.refetch()} />
            ) : analysis ? (
              <AnalysisPanel analysis={analysis} />
            ) : (
              <Alert color="gray" variant="light">
                No analysis is available for this run yet.
              </Alert>
            )}

            <Group justify="space-between" mt="xs">
              <Title order={2} size="h3">
                Proposed action{siblings.length ? 's' : ''}
              </Title>
              {supersededCount > 0 ? (
                <Switch
                  size="sm"
                  label={`Show superseded (${supersededCount})`}
                  checked={showSuperseded}
                  onChange={(event) => setShowSuperseded(event.currentTarget.checked)}
                />
              ) : null}
            </Group>
            <ActionCard actionId={current.id} focused={siblings.length > 0} />
            {visibleSiblings.map((sibling) => (
              <ActionCard key={sibling.id} actionId={sibling.id} />
            ))}
          </Stack>
        </Grid.Col>
      </Grid>

      {runId ? (
        <ConfirmModal
          opened={confirmOpen}
          onClose={confirm.close}
          title="Request re-analysis?"
          confirmLabel="Re-analyze"
          loading={reanalyze.isPending}
          onConfirm={() => reanalyze.mutate(runId, { onSuccess: () => confirm.close() })}
        >
          The email is analyzed again from scratch. Every open proposal of this run — including approved but
          not yet executed ones — is superseded and must be reviewed again. Completed actions are not
          affected.
        </ConfirmModal>
      ) : null}
    </Stack>
  );
}
