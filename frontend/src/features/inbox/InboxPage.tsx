import {
  ActionIcon,
  Anchor,
  Button,
  Card,
  Group,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Tooltip,
  VisuallyHidden,
} from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import {
  IconAlertTriangle,
  IconFilterOff,
  IconMailOpened,
  IconPaperclip,
  IconRefresh,
  IconRoute,
  IconSearch,
  IconSparkles,
} from '@tabler/icons-react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';

import { emailsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import {
  EMAIL_ANALYSIS_STATUSES,
  EMAIL_CATEGORIES,
  PRIORITIES,
  type EmailAnalysisStatus,
  type EmailCategory,
  type EmailListFilters,
  type EmailListItem,
  type Priority,
} from '../../api/types';
import { CategoryBadge, PriorityBadge } from '../../components/DomainBadges';
import { EmptyPanel } from '../../components/EmptyPanel';
import { ErrorState } from '../../components/ErrorState';
import { TableSkeleton } from '../../components/LoadingSkeleton';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { TablePagination } from '../../components/TablePagination';
import { TimeText } from '../../components/TimeText';
import { CATEGORY_LABELS, PRIORITY_LABELS } from '../../lib/format';
import { EMAIL_ANALYSIS_STATUS, POLL_INTERVAL_MS, isEmailInFlight } from '../../theme/status';
import { useHasPermission } from '../auth/session';
import { useAnalyzeEmail, useSyncMailbox } from './hooks';

const PAGE_SIZE = 25;

function pick<T extends string>(value: string | null, allowed: readonly T[]): T | undefined {
  return value && (allowed as readonly string[]).includes(value) ? (value as T) : undefined;
}

export function ContentChangedIndicator({ changed }: { changed: boolean }) {
  if (!changed) return null;
  return (
    <Tooltip label="The email content changed after it was analyzed. Re-analyze before approving.">
      <Group gap={4} wrap="nowrap" c="orange.7" component="span" style={{ display: 'inline-flex' }}>
        <IconAlertTriangle size={16} aria-hidden />
        <Text span size="xs" fw={600}>
          Changed
        </Text>
        <VisuallyHidden>— email content changed since analysis</VisuallyHidden>
      </Group>
    </Tooltip>
  );
}

function EmailRow({
  email,
  canAnalyze,
  analyzing,
  onAnalyze,
}: {
  email: EmailListItem;
  canAnalyze: boolean;
  analyzing: boolean;
  onAnalyze: () => void;
}) {
  const inFlight = isEmailInFlight(email.analysis_status);
  const analyzed = email.analysis_status !== 'not_analyzed';
  return (
    <Table.Tr>
      <Table.Td maw={220}>
        <Text size="sm" fw={email.is_read ? 400 : 650} truncate>
          {/* `||`: Graph can return an empty display name, which must fall back to the address. */}
          {email.sender_name || email.sender_address || 'Unknown sender'}
        </Text>
        {email.sender_name && email.sender_address ? (
          <Text size="xs" c="dimmed" truncate>
            {email.sender_address}
          </Text>
        ) : null}
      </Table.Td>
      <Table.Td maw={420}>
        <Group gap={6} wrap="nowrap">
          <Anchor
            component={Link}
            to={`/emails/${email.id}`}
            size="sm"
            fw={email.is_read ? 500 : 700}
            truncate
          >
            {email.subject || '(no subject)'}
          </Anchor>
          {email.has_attachments ? (
            <Tooltip label="Has attachments (metadata only)">
              <IconPaperclip size={14} aria-label="Has attachments" />
            </Tooltip>
          ) : null}
        </Group>
        <Text size="xs" c="dimmed" truncate>
          {email.body_preview}
        </Text>
      </Table.Td>
      <Table.Td>
        <TimeText value={email.received_at} size="sm" style={{ whiteSpace: 'nowrap' }} />
      </Table.Td>
      <Table.Td>
        <Stack gap={4} align="flex-start">
          <CategoryBadge category={email.category} size="sm" />
          <PriorityBadge priority={email.priority} size="sm" />
        </Stack>
      </Table.Td>
      <Table.Td>
        <Stack gap={4} align="flex-start">
          <StatusBadge kind="email" status={email.analysis_status} size="sm" />
          <ContentChangedIndicator changed={email.content_changed_since_analysis} />
        </Stack>
      </Table.Td>
      <Table.Td>
        <Group gap={4} wrap="nowrap" justify="flex-end">
          {email.latest_workflow_run_id ? (
            <Tooltip label="Open latest workflow">
              <ActionIcon
                component={Link}
                to={`/workflows/${email.latest_workflow_run_id}`}
                variant="subtle"
                aria-label={`Open latest workflow for “${email.subject}”`}
              >
                <IconRoute size={18} aria-hidden />
              </ActionIcon>
            </Tooltip>
          ) : null}
          {canAnalyze ? (
            <Button
              size="xs"
              variant={analyzed && !email.content_changed_since_analysis ? 'default' : 'light'}
              leftSection={<IconSparkles size={14} aria-hidden />}
              loading={analyzing}
              disabled={inFlight}
              onClick={onAnalyze}
              aria-label={`${analyzed ? 'Re-analyze' : 'Analyze'} “${email.subject}”`}
            >
              {analyzed ? 'Re-analyze' : 'Analyze'}
            </Button>
          ) : null}
        </Group>
      </Table.Td>
    </Table.Tr>
  );
}

export function InboxPage() {
  const [params, setParams] = useSearchParams();
  const canSync = useHasPermission('email:sync');
  const canAnalyze = useHasPermission('workflow:run');

  const [search, setSearch] = useState(params.get('q') ?? '');
  const [debouncedSearch] = useDebouncedValue(search.trim(), 350);

  const filters: EmailListFilters = {
    q: params.get('q') ?? undefined,
    category: pick<EmailCategory>(params.get('category'), EMAIL_CATEGORIES),
    priority: pick<Priority>(params.get('priority'), PRIORITIES),
    analysis_status: pick<EmailAnalysisStatus>(params.get('analysis_status'), EMAIL_ANALYSIS_STATUSES),
    limit: PAGE_SIZE,
    offset: Math.max(0, Number(params.get('offset') ?? 0) || 0),
  };
  const hasFilters = Boolean(filters.q || filters.category || filters.priority || filters.analysis_status);

  const updateParams = (patch: Record<string, string | null | undefined>) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(patch)) {
          if (value) next.set(key, value);
          else next.delete(key);
        }
        if (!('offset' in patch)) next.delete('offset');
        return next;
      },
      { replace: true },
    );
  };

  // Push the debounced search box into the URL (the URL is the source of truth for filters).
  // Only a *new* debounced value is pushed: comparing against the URL alone would re-apply the
  // stale value whenever the URL changes first (e.g. "Clear filters" before the debounce settles).
  const currentQ = params.get('q') ?? '';
  const pushedSearch = useRef(debouncedSearch);
  useEffect(() => {
    if (debouncedSearch === pushedSearch.current) return;
    pushedSearch.current = debouncedSearch;
    if (debouncedSearch === currentQ) return;
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (debouncedSearch) next.set('q', debouncedSearch);
        else next.delete('q');
        next.delete('offset');
        return next;
      },
      { replace: true },
    );
  }, [debouncedSearch, currentQ, setParams]);

  const emails = useQuery({
    queryKey: queryKeys.emailList(filters),
    queryFn: () => emailsApi.list(filters),
    placeholderData: keepPreviousData,
    refetchInterval: (query) =>
      query.state.data?.items.some((e) => isEmailInFlight(e.analysis_status)) ? POLL_INTERVAL_MS : false,
  });

  const sync = useSyncMailbox();
  const analyze = useAnalyzeEmail();

  const syncButton = canSync ? (
    <Button
      leftSection={<IconRefresh size={18} aria-hidden />}
      onClick={() => sync.mutate(undefined)}
      loading={sync.isPending}
    >
      Sync mailbox
    </Button>
  ) : null;

  return (
    <>
      <PageHeader
        title="Inbox"
        description="Email synced from connected mailboxes. Bodies are stored as plain text and analyzed only when you ask (or when auto-analyze is enabled)."
        actions={syncButton}
      />
      <Card padding="md">
        <Group gap="sm" mb="md" wrap="wrap" align="flex-end">
          <TextInput
            label="Search"
            placeholder="Subject or sender"
            leftSection={<IconSearch size={16} aria-hidden />}
            value={search}
            onChange={(event) => setSearch(event.currentTarget.value)}
            style={{ flex: '1 1 240px' }}
          />
          <Select
            label="Category"
            placeholder="All categories"
            clearable
            data={EMAIL_CATEGORIES.map((c) => ({ value: c, label: CATEGORY_LABELS[c] }))}
            value={filters.category ?? null}
            onChange={(value) => updateParams({ category: value })}
            w={190}
          />
          <Select
            label="Priority"
            placeholder="All priorities"
            clearable
            data={PRIORITIES.map((p) => ({ value: p, label: PRIORITY_LABELS[p] }))}
            value={filters.priority ?? null}
            onChange={(value) => updateParams({ priority: value })}
            w={160}
          />
          <Select
            label="Analysis status"
            placeholder="Any status"
            clearable
            data={EMAIL_ANALYSIS_STATUSES.map((s) => ({ value: s, label: EMAIL_ANALYSIS_STATUS[s].label }))}
            value={filters.analysis_status ?? null}
            onChange={(value) => updateParams({ analysis_status: value })}
            w={180}
          />
          {hasFilters ? (
            <Button
              variant="subtle"
              leftSection={<IconFilterOff size={16} aria-hidden />}
              onClick={() => {
                setSearch('');
                setParams(new URLSearchParams(), { replace: true });
              }}
            >
              Clear filters
            </Button>
          ) : null}
        </Group>

        {emails.isPending ? (
          <TableSkeleton label="Loading emails" />
        ) : emails.isError ? (
          <ErrorState
            error={emails.error}
            onRetry={() => void emails.refetch()}
            retrying={emails.isFetching}
          />
        ) : emails.data.total === 0 ? (
          hasFilters ? (
            <EmptyPanel
              icon={<IconFilterOff size={28} aria-hidden />}
              title="No emails match these filters"
              description="Try a broader search or clear the filters."
            />
          ) : (
            <EmptyPanel
              icon={<IconMailOpened size={28} aria-hidden />}
              title="No emails yet — sync your mailbox"
              description={
                canSync
                  ? 'Pull the latest messages from your connected mailbox. In demo mode a synthetic mailbox is used.'
                  : 'Ask a member or admin to connect and sync a mailbox.'
              }
              action={
                <Group>
                  {syncButton}
                  <Button component={Link} to="/integrations" variant="default">
                    Integrations
                  </Button>
                </Group>
              }
            />
          )
        ) : (
          <>
            <Table.ScrollContainer minWidth={960}>
              <Table aria-label="Emails" aria-busy={emails.isFetching}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Sender</Table.Th>
                    <Table.Th>Subject</Table.Th>
                    <Table.Th>Received</Table.Th>
                    <Table.Th>Category / priority</Table.Th>
                    <Table.Th>Analysis</Table.Th>
                    <Table.Th>
                      <VisuallyHidden>Row actions</VisuallyHidden>
                    </Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {emails.data.items.map((email) => (
                    <EmailRow
                      key={email.id}
                      email={email}
                      canAnalyze={canAnalyze}
                      analyzing={analyze.isPending && analyze.variables === email.id}
                      onAnalyze={() => analyze.mutate(email.id)}
                    />
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
            <TablePagination
              total={emails.data.total}
              limit={emails.data.limit || PAGE_SIZE}
              offset={emails.data.offset}
              onOffsetChange={(offset) => updateParams({ offset: offset ? String(offset) : null })}
            />
          </>
        )}
      </Card>
    </>
  );
}
