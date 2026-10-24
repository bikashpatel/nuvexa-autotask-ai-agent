import {
  ActionIcon,
  Anchor,
  Button,
  Card,
  Code,
  Collapse,
  Group,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  VisuallyHidden,
} from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import { IconChevronDown, IconChevronRight, IconFilterOff, IconHistory } from '@tabler/icons-react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Fragment, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';

import { auditApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import { AUDIT_OUTCOMES, type AuditEventOut, type AuditOutcome } from '../../api/types';
import { EmptyPanel } from '../../components/EmptyPanel';
import { ErrorState } from '../../components/ErrorState';
import { JsonBlock } from '../../components/JsonBlock';
import { TableSkeleton } from '../../components/LoadingSkeleton';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { TablePagination } from '../../components/TablePagination';
import { TimeText } from '../../components/TimeText';
import { formatDateTime, humanize, shortId } from '../../lib/format';
import { AUDIT_OUTCOME } from '../../theme/status';
import { useHasPermission } from '../auth/session';

const PAGE_SIZE = 50;
const RESOURCE_TYPES = [
  'action',
  'workflow_run',
  'email',
  'connection',
  'settings',
  'session',
  'user',
  'task',
];

function AuditRow({ event }: { event: AuditEventOut }) {
  const [open, setOpen] = useState(false);
  const detailsId = `audit-details-${event.id}`;
  return (
    <Fragment>
      <Table.Tr>
        <Table.Td w={40}>
          <ActionIcon
            variant="subtle"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-controls={detailsId}
            aria-label={`${open ? 'Hide' : 'Show'} details for ${humanize(event.event_type)}`}
          >
            {open ? <IconChevronDown size={16} aria-hidden /> : <IconChevronRight size={16} aria-hidden />}
          </ActionIcon>
        </Table.Td>
        <Table.Td>
          <TimeText value={event.created_at} size="sm" style={{ whiteSpace: 'nowrap' }} />
        </Table.Td>
        <Table.Td>
          <Text size="sm" fw={500}>
            {humanize(event.event_type)}
          </Text>
          <Text size="xs" c="dimmed" ff="monospace">
            {event.event_type}
          </Text>
        </Table.Td>
        <Table.Td>
          <Text size="sm">{event.actor_name ?? humanize(event.actor_type)}</Text>
          <Text size="xs" c="dimmed">
            {humanize(event.actor_type)}
          </Text>
        </Table.Td>
        <Table.Td>
          <Text size="sm">{humanize(event.resource_type)}</Text>
          {event.action_id ? (
            <Anchor component={Link} to={`/actions/${event.action_id}`} size="xs">
              action {shortId(event.action_id)}
            </Anchor>
          ) : event.workflow_run_id ? (
            <Anchor component={Link} to={`/workflows/${event.workflow_run_id}`} size="xs">
              run {shortId(event.workflow_run_id)}
            </Anchor>
          ) : event.resource_id ? (
            <Text size="xs" c="dimmed" ff="monospace">
              {shortId(event.resource_id)}
            </Text>
          ) : null}
        </Table.Td>
        <Table.Td>
          <StatusBadge kind="audit" status={event.outcome} size="sm" />
        </Table.Td>
      </Table.Tr>
      <Table.Tr style={{ display: open ? undefined : 'none' }}>
        <Table.Td colSpan={6} p={0}>
          <Collapse expanded={open} id={detailsId}>
            <Stack gap="xs" p="sm">
              <Group gap="lg" wrap="wrap">
                <Text size="xs">
                  <Text span c="dimmed">
                    Time{' '}
                  </Text>
                  {formatDateTime(event.created_at)}
                </Text>
                <Text size="xs">
                  <Text span c="dimmed">
                    Correlation{' '}
                  </Text>
                  <Code fz="xs">{event.correlation_id ?? '—'}</Code>
                </Text>
                <Text size="xs">
                  <Text span c="dimmed">
                    Resource{' '}
                  </Text>
                  <Code fz="xs">{event.resource_id ?? '—'}</Code>
                </Text>
              </Group>
              <JsonBlock value={event.details} />
            </Stack>
          </Collapse>
        </Table.Td>
      </Table.Tr>
    </Fragment>
  );
}

export function AuditLogPage() {
  const [params, setParams] = useSearchParams();
  const tenantWide = useHasPermission('audit:read_all');
  const [eventType, setEventType] = useState(params.get('event_type') ?? '');
  const [debouncedEventType] = useDebouncedValue(eventType.trim(), 350);
  const outcomeParam = params.get('outcome');
  const outcome = (AUDIT_OUTCOMES as readonly string[]).includes(outcomeParam ?? '')
    ? (outcomeParam as AuditOutcome)
    : undefined;
  const resourceType = params.get('resource_type') ?? undefined;
  const offset = Math.max(0, Number(params.get('offset') ?? 0) || 0);
  const currentEventType = params.get('event_type') ?? '';

  // Only a *new* debounced value is pushed into the URL; comparing against the URL alone would
  // re-apply the stale value when the URL changes first (e.g. "Clear filters").
  const pushedEventType = useRef(debouncedEventType);
  useEffect(() => {
    if (debouncedEventType === pushedEventType.current) return;
    pushedEventType.current = debouncedEventType;
    if (debouncedEventType === currentEventType) return;
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (debouncedEventType) next.set('event_type', debouncedEventType);
        else next.delete('event_type');
        next.delete('offset');
        return next;
      },
      { replace: true },
    );
  }, [debouncedEventType, currentEventType, setParams]);

  const query = {
    event_type: currentEventType || undefined,
    resource_type: resourceType,
    outcome,
    limit: PAGE_SIZE,
    offset,
  };
  const events = useQuery({
    queryKey: queryKeys.audit(query),
    queryFn: () => auditApi.list(query),
    placeholderData: keepPreviousData,
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
  const hasFilters = Boolean(currentEventType || resourceType || outcome);

  return (
    <>
      <PageHeader
        title="Audit log"
        description={
          tenantWide
            ? 'Every approval, rejection, permission failure and external write across the tenant.'
            : 'Audit events for your own resources.'
        }
      />
      <Card padding="md">
        <Group gap="sm" mb="md" align="flex-end" wrap="wrap">
          <TextInput
            label="Event type"
            placeholder="e.g. action.approved"
            value={eventType}
            onChange={(e) => setEventType(e.currentTarget.value)}
            w={240}
          />
          <Select
            label="Resource type"
            placeholder="Any resource"
            clearable
            searchable
            data={Array.from(new Set([...RESOURCE_TYPES, ...(resourceType ? [resourceType] : [])])).map(
              (r) => ({
                value: r,
                label: humanize(r),
              }),
            )}
            value={resourceType ?? null}
            onChange={(value) => update('resource_type', value)}
            w={200}
          />
          <Select
            label="Outcome"
            placeholder="Any outcome"
            clearable
            data={AUDIT_OUTCOMES.map((o) => ({ value: o, label: AUDIT_OUTCOME[o].label }))}
            value={outcome ?? null}
            onChange={(value) => update('outcome', value)}
            w={160}
          />
          {hasFilters ? (
            <Button
              variant="subtle"
              leftSection={<IconFilterOff size={16} aria-hidden />}
              onClick={() => {
                setEventType('');
                setParams(new URLSearchParams(), { replace: true });
              }}
            >
              Clear filters
            </Button>
          ) : null}
        </Group>
        {events.isPending ? (
          <TableSkeleton label="Loading audit events" />
        ) : events.isError ? (
          <ErrorState
            error={events.error}
            onRetry={() => void events.refetch()}
            retrying={events.isFetching}
          />
        ) : events.data.total === 0 ? (
          <EmptyPanel
            icon={<IconHistory size={28} aria-hidden />}
            title={hasFilters ? 'No audit events match these filters' : 'No audit events yet'}
            description="Approvals, rejections, executions and permission failures are recorded here."
          />
        ) : (
          <>
            <Table.ScrollContainer minWidth={820}>
              <Table aria-label="Audit events" highlightOnHover={false}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>
                      <VisuallyHidden>Expand</VisuallyHidden>
                    </Table.Th>
                    <Table.Th>When</Table.Th>
                    <Table.Th>Event</Table.Th>
                    <Table.Th>Actor</Table.Th>
                    <Table.Th>Resource</Table.Th>
                    <Table.Th>Outcome</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {events.data.items.map((event) => (
                    <AuditRow key={event.id} event={event} />
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
            <TablePagination
              total={events.data.total}
              limit={events.data.limit || PAGE_SIZE}
              offset={events.data.offset}
              onOffsetChange={(next) => update('offset', next ? String(next) : null)}
            />
          </>
        )}
      </Card>
    </>
  );
}
