import {
  Anchor,
  Badge,
  Button,
  Grid,
  Group,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  Timeline,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconBolt,
  IconCircleCheck,
  IconClipboardCheck,
  IconClockHour4,
  IconCoin,
  IconHistory,
  IconMailOpened,
  IconSparkles,
  IconX,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';

import { dashboardApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import { WORKFLOW_STATUSES, type AuditEventOut } from '../../api/types';
import { DistributionBars, type DistributionItem } from '../../components/DistributionBars';
import { EmptyPanel } from '../../components/EmptyPanel';
import { ErrorState } from '../../components/ErrorState';
import { PageHeader } from '../../components/PageHeader';
import { SectionCard } from '../../components/SectionCard';
import { StatTile } from '../../components/StatTile';
import { StatusBadge } from '../../components/StatusBadge';
import { TimeText } from '../../components/TimeText';
import {
  categoryLabel,
  formatCompact,
  formatCurrency,
  formatDuration,
  formatLatency,
  humanize,
} from '../../lib/format';
import { getStatusDescriptor } from '../../theme/status';
import { useSession } from '../auth/session';

function activityLink(event: AuditEventOut): string | null {
  if (event.action_id) return `/actions/${event.action_id}`;
  if (event.workflow_run_id) return `/workflows/${event.workflow_run_id}`;
  return null;
}

export function ActivityFeed({ events }: { events: AuditEventOut[] }) {
  if (events.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No activity yet. Sync a mailbox and analyze an email to get started.
      </Text>
    );
  }
  return (
    <Timeline bulletSize={22} lineWidth={2} active={-1}>
      {events.map((event) => {
        const to = activityLink(event);
        const outcome = getStatusDescriptor('audit', event.outcome);
        return (
          <Timeline.Item
            key={event.id}
            color={outcome.color}
            bullet={
              event.outcome === 'success' ? (
                <IconCircleCheck size={12} aria-hidden />
              ) : event.outcome === 'denied' ? (
                <IconX size={12} aria-hidden />
              ) : (
                <IconAlertTriangle size={12} aria-hidden />
              )
            }
            title={
              <Group gap="xs" wrap="wrap">
                {to ? (
                  <Anchor component={Link} to={to} size="sm" fw={600}>
                    {humanize(event.event_type)}
                  </Anchor>
                ) : (
                  <Text size="sm" fw={600}>
                    {humanize(event.event_type)}
                  </Text>
                )}
                <StatusBadge kind="audit" status={event.outcome} size="xs" />
              </Group>
            }
          >
            <Text size="xs" c="dimmed">
              {event.actor_name ?? humanize(event.actor_type)} · {humanize(event.resource_type)} ·{' '}
              <TimeText value={event.created_at} size="xs" />
            </Text>
          </Timeline.Item>
        );
      })}
    </Timeline>
  );
}

export function OverviewPage() {
  const { user } = useSession();
  const summary = useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: dashboardApi.summary,
    refetchInterval: 15_000,
  });

  const header = (
    <PageHeader
      eyebrow={`${user.tenant_name} · operations overview`}
      title={`Welcome back, ${user.display_name.split(' ')[0] ?? user.display_name}`}
      description="Email-to-work pipeline health: what was analyzed, what is waiting for a decision, and what was written to target systems."
      actions={
        <Button component={Link} to="/approvals" leftSection={<IconClipboardCheck size={18} aria-hidden />}>
          Review approvals
        </Button>
      }
    />
  );

  if (summary.isPending) {
    return (
      <>
        {header}
        <SimpleGrid
          cols={{ base: 1, xs: 2, md: 3, xl: 6 }}
          spacing="md"
          role="status"
          aria-busy="true"
          aria-label="Loading KPIs"
        >
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} height={118} />
          ))}
        </SimpleGrid>
        <Skeleton height={260} mt="md" />
      </>
    );
  }
  if (summary.isError) {
    return (
      <>
        {header}
        <ErrorState
          error={summary.error}
          onRetry={() => void summary.refetch()}
          retrying={summary.isFetching}
        />
      </>
    );
  }

  const data = summary.data;
  const failedOrUncertain = data.failed_actions + data.uncertain_actions;
  const workflowItems: DistributionItem[] = WORKFLOW_STATUSES.filter(
    (s) => (data.workflows_by_status[s] ?? 0) > 0,
  )
    .map((status): DistributionItem => ({
      key: status,
      text: getStatusDescriptor('workflow', status).label,
      label: <StatusBadge kind="workflow" status={status} size="sm" />,
      value: data.workflows_by_status[status] ?? 0,
      color: `var(--mantine-color-${getStatusDescriptor('workflow', status).color}-5)`,
    }))
    .concat(
      Object.entries(data.workflows_by_status)
        .filter(([status, count]) => !(WORKFLOW_STATUSES as readonly string[]).includes(status) && count > 0)
        .map(([status, count]) => ({
          key: status,
          text: humanize(status),
          label: <StatusBadge kind="workflow" status={status} size="sm" />,
          value: count,
          color: 'var(--mantine-color-gray-5)',
        })),
    );
  const categoryItems = Object.entries(data.categories)
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([category, count]) => ({
      key: category,
      text: categoryLabel(category),
      label: (
        <Text size="sm" truncate>
          {categoryLabel(category)}
        </Text>
      ),
      value: count,
    }));

  if (data.emails_total === 0 && data.actions_proposed === 0) {
    return (
      <>
        {header}
        <EmptyPanel
          icon={<IconMailOpened size={28} aria-hidden />}
          title="No emails yet — sync your mailbox"
          description="Connect a Microsoft 365 mailbox (or use the demo mailbox), sync it, and analyze an email. KPIs appear here as work flows through the pipeline."
          action={
            <Group>
              <Button component={Link} to="/inbox">
                Go to inbox
              </Button>
              <Button component={Link} to="/integrations" variant="default">
                Integrations
              </Button>
            </Group>
          }
        />
      </>
    );
  }

  return (
    <Stack gap="md">
      {header}
      <SimpleGrid
        cols={{ base: 1, xs: 2, md: 3, xl: 6 }}
        spacing="md"
        component="section"
        aria-label="Key metrics"
      >
        <StatTile
          label="Emails analyzed"
          value={formatCompact(data.emails_analyzed)}
          hint={`of ${formatCompact(data.emails_total)} synced`}
          icon={<IconMailOpened size={18} />}
          to="/inbox"
        />
        <StatTile
          label="Actions proposed"
          value={formatCompact(data.actions_proposed)}
          hint="Work items, replies, follow-ups"
          icon={<IconSparkles size={18} />}
        />
        <StatTile
          label="Pending approvals"
          value={formatCompact(data.pending_approvals)}
          hint="Waiting for a human decision"
          icon={<IconClipboardCheck size={18} />}
          accent="orange"
          to="/approvals"
        />
        <StatTile
          label="Completed"
          value={formatCompact(data.completed_actions)}
          hint="Executed after approval"
          icon={<IconCircleCheck size={18} />}
          accent="green"
        />
        <StatTile
          label="Failed / uncertain"
          value={formatCompact(failedOrUncertain)}
          hint={`${formatCompact(data.failed_actions)} failed · ${formatCompact(data.uncertain_actions)} uncertain`}
          icon={<IconAlertTriangle size={18} />}
          accent="red"
          to="/approvals?view=attention"
        />
        <StatTile
          label="Rejected"
          value={formatCompact(data.rejected_actions)}
          hint="Declined by reviewers"
          icon={<IconX size={18} />}
          accent="gray"
        />
      </SimpleGrid>

      <Grid gap="md">
        <Grid.Col span={{ base: 12, lg: 6 }}>
          <SectionCard
            title="Workflows by status"
            id="workflows-by-status"
            description="Analysis runs across the pipeline."
          >
            <DistributionBars
              items={workflowItems}
              ariaLabel="Workflows by status"
              emptyText="No workflow runs yet."
            />
          </SectionCard>
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 6 }}>
          <SectionCard title="Categories" id="categories" description="How analyzed email was classified.">
            <DistributionBars
              items={categoryItems}
              ariaLabel="Email categories"
              emptyText="No classified email yet."
            />
          </SectionCard>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 6, lg: 4 }}>
          <Stack gap="md" h="100%">
            <StatTile
              label="Average approval wait"
              value={formatDuration(data.avg_approval_wait_seconds)}
              hint="From proposal to human decision"
              icon={<IconClockHour4 size={18} />}
              accent="blue"
            />
            <SectionCard title="LLM usage" id="llm-usage" icon={<IconBolt size={18} aria-hidden />}>
              <SimpleGrid cols={3} spacing="xs">
                <div>
                  <Text size="xs" c="dimmed">
                    Tokens
                  </Text>
                  <Text fw={650} size="lg">
                    {formatCompact(data.llm.total_tokens)}
                  </Text>
                </div>
                <div>
                  <Text size="xs" c="dimmed">
                    Est. cost
                  </Text>
                  <Group gap={4} wrap="nowrap">
                    <IconCoin size={14} aria-hidden />
                    <Text fw={650} size="lg">
                      {formatCurrency(data.llm.estimated_cost_usd)}
                    </Text>
                  </Group>
                </div>
                <div>
                  <Text size="xs" c="dimmed">
                    Avg latency
                  </Text>
                  <Text fw={650} size="lg">
                    {formatLatency(data.llm.avg_latency_ms)}
                  </Text>
                </div>
              </SimpleGrid>
              <Text size="xs" c="dimmed" mt="sm">
                Cost is estimated from configured per-million-token prices.
              </Text>
            </SectionCard>
          </Stack>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 6, lg: 8 }}>
          <SectionCard
            title="Recent activity"
            id="recent-activity"
            icon={<IconHistory size={18} aria-hidden />}
            actions={
              <Badge variant="light" color="gray">
                Audit events
              </Badge>
            }
          >
            <ActivityFeed events={data.recent_activity} />
            <Anchor component={Link} to="/audit" size="sm" mt="md" display="inline-block">
              Open the audit log
            </Anchor>
          </SectionCard>
        </Grid.Col>
      </Grid>
    </Stack>
  );
}
