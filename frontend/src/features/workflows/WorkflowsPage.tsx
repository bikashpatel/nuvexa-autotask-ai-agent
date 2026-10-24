import { Anchor, Card, Group, Select, Table, Text } from '@mantine/core';
import { IconRoute } from '@tabler/icons-react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router';

import { workflowsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import { WORKFLOW_STATUSES, type WorkflowStatus } from '../../api/types';
import { EmptyPanel } from '../../components/EmptyPanel';
import { ErrorState } from '../../components/ErrorState';
import { TableSkeleton } from '../../components/LoadingSkeleton';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { TablePagination } from '../../components/TablePagination';
import { TimeText } from '../../components/TimeText';
import { humanize } from '../../lib/format';
import { POLL_INTERVAL_MS, WORKFLOW_STATUS, isWorkflowInFlight } from '../../theme/status';

const PAGE_SIZE = 25;

export function WorkflowsPage() {
  const [params, setParams] = useSearchParams();
  const statusParam = params.get('status');
  const status = (WORKFLOW_STATUSES as readonly string[]).includes(statusParam ?? '')
    ? (statusParam as WorkflowStatus)
    : undefined;
  const offset = Math.max(0, Number(params.get('offset') ?? 0) || 0);
  const query = { status, limit: PAGE_SIZE, offset };

  const workflows = useQuery({
    queryKey: queryKeys.workflowList(query),
    queryFn: () => workflowsApi.list(query),
    placeholderData: keepPreviousData,
    refetchInterval: (q) =>
      q.state.data?.items.some((r) => isWorkflowInFlight(r.status)) ? POLL_INTERVAL_MS : false,
  });

  const update = (key: string, value: string | null) =>
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

  return (
    <>
      <PageHeader
        title="Workflows"
        description="Every analysis run: from ingestion through classification, extraction, validation, proposal, approval and execution."
      />
      <Card padding="md">
        <Group mb="md">
          <Select
            label="Status"
            placeholder="Any status"
            clearable
            data={WORKFLOW_STATUSES.map((s) => ({ value: s, label: WORKFLOW_STATUS[s].label }))}
            value={status ?? null}
            onChange={(value) => update('status', value)}
            w={220}
          />
        </Group>
        {workflows.isPending ? (
          <TableSkeleton label="Loading workflows" />
        ) : workflows.isError ? (
          <ErrorState
            error={workflows.error}
            onRetry={() => void workflows.refetch()}
            retrying={workflows.isFetching}
          />
        ) : workflows.data.total === 0 ? (
          <EmptyPanel
            icon={<IconRoute size={28} aria-hidden />}
            title={status ? 'No workflows with this status' : 'No workflow runs yet'}
            description="Analyze an email from the inbox to start a workflow."
          />
        ) : (
          <>
            <Table.ScrollContainer minWidth={900}>
              <Table aria-label="Workflow runs">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Email</Table.Th>
                    <Table.Th>Status</Table.Th>
                    <Table.Th>Step</Table.Th>
                    <Table.Th>Attempt</Table.Th>
                    <Table.Th>Model</Table.Th>
                    <Table.Th>Updated</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {workflows.data.items.map((run) => (
                    <Table.Tr key={run.id}>
                      <Table.Td maw={380}>
                        <Anchor component={Link} to={`/workflows/${run.id}`} size="sm" fw={600} lineClamp={1}>
                          {run.email_subject ?? run.id}
                        </Anchor>
                        <Text size="xs" c="dimmed">
                          {humanize(run.trigger)} trigger
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <StatusBadge kind="workflow" status={run.status} size="sm" />
                      </Table.Td>
                      <Table.Td>{humanize(run.current_step)}</Table.Td>
                      <Table.Td className="nuvexa-tabular">#{run.analysis_attempt}</Table.Td>
                      <Table.Td>
                        <Text size="sm" truncate maw={200}>
                          {[run.model_provider, run.model_name].filter(Boolean).join(' / ') || '—'}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <TimeText value={run.updated_at} size="sm" />
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
            <TablePagination
              total={workflows.data.total}
              limit={workflows.data.limit || PAGE_SIZE}
              offset={workflows.data.offset}
              onOffsetChange={(next) => update('offset', next ? String(next) : null)}
            />
          </>
        )}
      </Card>
    </>
  );
}
