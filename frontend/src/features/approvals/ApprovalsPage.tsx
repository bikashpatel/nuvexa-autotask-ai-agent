import {
  Anchor,
  Button,
  Card,
  Group,
  SegmentedControl,
  Select,
  Skeleton,
  Stack,
  Table,
  Text,
  VisuallyHidden,
} from '@mantine/core';
import { IconArrowRight, IconChecks, IconClipboardCheck } from '@tabler/icons-react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router';

import { actionsApi, workflowsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import { ACTION_TYPES, type ActionOut, type ActionStatus, type ActionType } from '../../api/types';
import { ConfidenceIndicator } from '../../components/ConfidenceIndicator';
import { EmptyPanel } from '../../components/EmptyPanel';
import { ErrorState } from '../../components/ErrorState';
import { TableSkeleton } from '../../components/LoadingSkeleton';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { TablePagination } from '../../components/TablePagination';
import { TimeText } from '../../components/TimeText';
import { ACTION_TYPE_LABELS } from '../../lib/format';
import { useHasPermission } from '../auth/session';

const PAGE_SIZE = 25;

type View = 'pending' | 'approved' | 'attention';

const VIEWS: Record<View, { label: string; statuses: ActionStatus[]; empty: string; hint: string }> = {
  pending: {
    label: 'Pending approval',
    statuses: ['pending_approval'],
    empty: 'Nothing is waiting for approval',
    hint: 'New proposals appear here after an email is analyzed.',
  },
  approved: {
    label: 'Approved — ready to execute',
    statuses: ['approved'],
    empty: 'No approved actions waiting for execution',
    hint: 'Approved actions are executed explicitly from the review screen.',
  },
  attention: {
    label: 'Needs attention',
    statuses: ['failed', 'uncertain'],
    empty: 'No failed or uncertain actions',
    hint: 'Failed executions can be retried; uncertain ones must be reconciled first.',
  },
};

/** Confidence lives on the run's analysis (not on ActionOut), fetched lazily per run and cached. */
function RunConfidence({ runId }: { runId: string }) {
  const analysis = useQuery({
    queryKey: queryKeys.workflowAnalysis(runId),
    queryFn: () => workflowsApi.analysis(runId),
    staleTime: 60_000,
    retry: false,
  });
  if (analysis.isPending) return <Skeleton height={20} width={80} />;
  if (analysis.isError)
    return (
      <Text span c="dimmed">
        —
      </Text>
    );
  return (
    <ConfidenceIndicator
      compact
      score={analysis.data.confidence_score}
      bucket={analysis.data.confidence_bucket}
      method={analysis.data.confidence_method}
    />
  );
}

function ActionRow({ action }: { action: ActionOut }) {
  return (
    <Table.Tr>
      <Table.Td>
        <Text size="sm">{ACTION_TYPE_LABELS[action.action_type] ?? action.action_type}</Text>
      </Table.Td>
      <Table.Td maw={340}>
        <Anchor component={Link} to={`/actions/${action.id}`} size="sm" fw={600} lineClamp={2}>
          {action.title}
        </Anchor>
      </Table.Td>
      <Table.Td maw={280}>
        <Text size="sm" c="dimmed" truncate>
          {action.email_subject ?? '—'}
        </Text>
      </Table.Td>
      <Table.Td>
        <RunConfidence runId={action.workflow_run_id} />
      </Table.Td>
      <Table.Td>
        <StatusBadge kind="action" status={action.status} size="sm" />
      </Table.Td>
      <Table.Td>
        <TimeText value={action.created_at} size="sm" style={{ whiteSpace: 'nowrap' }} />
      </Table.Td>
      <Table.Td>
        <Button
          component={Link}
          to={`/actions/${action.id}`}
          size="xs"
          variant="light"
          rightSection={<IconArrowRight size={14} aria-hidden />}
          aria-label={`Review “${action.title}”`}
        >
          Review
        </Button>
      </Table.Td>
    </Table.Tr>
  );
}

export function ApprovalsPage() {
  const [params, setParams] = useSearchParams();
  const canSeeAll = useHasPermission('action:approve_any');
  const viewParam = params.get('view');
  const view: View = viewParam === 'approved' || viewParam === 'attention' ? viewParam : 'pending';
  const typeParam = params.get('type');
  const actionType = (ACTION_TYPES as readonly string[]).includes(typeParam ?? '')
    ? (typeParam as ActionType)
    : undefined;
  const offset = Math.max(0, Number(params.get('offset') ?? 0) || 0);
  const config = VIEWS[view];

  const query = { status: config.statuses, action_type: actionType, limit: PAGE_SIZE, offset };
  const actions = useQuery({
    queryKey: queryKeys.actionList(query),
    queryFn: () => actionsApi.list(query),
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  });

  const setParam = (key: string, value: string | null) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== 'offset') next.delete('offset');
        return next;
      },
      { replace: true },
    );
  };

  return (
    <>
      <PageHeader
        title="Approvals"
        description={
          canSeeAll
            ? 'Every proposal across the tenant that needs a human decision. Nothing is executed without an approval bound to the exact payload.'
            : 'Your proposals that need a decision. Nothing is executed without an approval bound to the exact payload.'
        }
      />
      <Card padding="md">
        <Group justify="space-between" mb="md" wrap="wrap" gap="sm">
          <SegmentedControl
            value={view}
            onChange={(value) => setParam('view', value === 'pending' ? null : value)}
            data={(Object.keys(VIEWS) as View[]).map((key) => ({ value: key, label: VIEWS[key].label }))}
            aria-label="Approval queue view"
          />
          <Select
            placeholder="All action types"
            aria-label="Filter by action type"
            clearable
            data={ACTION_TYPES.map((t) => ({ value: t, label: ACTION_TYPE_LABELS[t] }))}
            value={actionType ?? null}
            onChange={(value) => setParam('type', value)}
            w={240}
          />
        </Group>
        {actions.isPending ? (
          <TableSkeleton label="Loading approvals" />
        ) : actions.isError ? (
          <ErrorState
            error={actions.error}
            onRetry={() => void actions.refetch()}
            retrying={actions.isFetching}
          />
        ) : actions.data.total === 0 ? (
          <EmptyPanel
            icon={
              view === 'pending' ? (
                <IconClipboardCheck size={28} aria-hidden />
              ) : (
                <IconChecks size={28} aria-hidden />
              )
            }
            title={config.empty}
            description={config.hint}
            action={
              view === 'pending' ? (
                <Button component={Link} to="/inbox" variant="light">
                  Go to inbox
                </Button>
              ) : undefined
            }
          />
        ) : (
          <Stack gap={0}>
            <Table.ScrollContainer minWidth={980}>
              <Table aria-label={config.label}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Type</Table.Th>
                    <Table.Th>Title</Table.Th>
                    <Table.Th>Email subject</Table.Th>
                    <Table.Th>Confidence</Table.Th>
                    <Table.Th>Status</Table.Th>
                    <Table.Th>Created</Table.Th>
                    <Table.Th>
                      <VisuallyHidden>Review</VisuallyHidden>
                    </Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {actions.data.items.map((action) => (
                    <ActionRow key={action.id} action={action} />
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
            <TablePagination
              total={actions.data.total}
              limit={actions.data.limit || PAGE_SIZE}
              offset={actions.data.offset}
              onOffsetChange={(next) => setParam('offset', next ? String(next) : null)}
            />
          </Stack>
        )}
      </Card>
    </>
  );
}
