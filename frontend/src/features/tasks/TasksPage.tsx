import {
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  CopyButton,
  Group,
  SegmentedControl,
  SimpleGrid,
  Skeleton,
  Stack,
  Tabs,
  Text,
  Title,
  Tooltip,
} from '@mantine/core';
import {
  IconBan,
  IconCheck,
  IconChecklist,
  IconCopy,
  IconMailForward,
  IconMailOff,
} from '@tabler/icons-react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import { Link } from 'react-router';

import { replyDraftsApi, tasksApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import { TASK_STATUSES, type FollowUpTaskOut, type ReplyDraftOut, type TaskStatus } from '../../api/types';
import { EmptyPanel } from '../../components/EmptyPanel';
import { ErrorState } from '../../components/ErrorState';
import { PlainText } from '../../components/PlainText';
import { StatusBadge } from '../../components/StatusBadge';
import { TablePagination } from '../../components/TablePagination';
import { PageHeader } from '../../components/PageHeader';
import { TimeText } from '../../components/TimeText';
import { notifyError, notifySuccess } from '../../lib/notify';
import { TASK_STATUS } from '../../theme/status';
import { useHasPermission } from '../auth/session';
import { dayjs } from '../../lib/format';

const PAGE_SIZE = 25;

function TaskCard({ task }: { task: FollowUpTaskOut }) {
  const queryClient = useQueryClient();
  const canEdit = useHasPermission('action:edit');
  // Not optimistic: the card changes only after the server confirms the new status.
  const update = useMutation({
    mutationFn: (status: TaskStatus) => tasksApi.update(task.id, status),
    onSuccess: (_, status) => {
      notifySuccess(
        status === 'done' ? 'Task marked done' : status === 'dismissed' ? 'Task dismissed' : 'Task reopened',
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.tasks });
    },
    onError: (error) => notifyError('Could not update the task', error),
  });
  const overdue = task.status === 'open' && task.due_at && dayjs(task.due_at).isBefore(dayjs());
  return (
    <Card padding="md" component="article" aria-labelledby={`task-${task.id}`}>
      <Stack gap="sm" h="100%">
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <Title order={3} size="h4" id={`task-${task.id}`} style={{ overflowWrap: 'anywhere' }}>
            {task.title}
          </Title>
          <StatusBadge kind="task" status={task.status} size="sm" />
        </Group>
        {task.description ? <PlainText c="dimmed">{task.description}</PlainText> : null}
        <Group gap="xs">
          {task.due_at ? (
            <Badge color={overdue ? 'red' : 'gray'} variant={overdue ? 'light' : 'outline'}>
              {overdue ? 'Overdue · ' : 'Due '}
              {dayjs(task.due_at).format('MMM D, YYYY HH:mm')}
            </Badge>
          ) : (
            <Badge color="gray" variant="outline">
              No due date
            </Badge>
          )}
          <Text size="xs" c="dimmed">
            Created <TimeText value={task.created_at} size="xs" />
          </Text>
        </Group>
        <Group gap="xs" mt="auto">
          {task.action_id ? (
            <Anchor component={Link} to={`/actions/${task.action_id}`} size="xs">
              Source action
            </Anchor>
          ) : null}
          {task.email_message_id ? (
            <Anchor component={Link} to={`/emails/${task.email_message_id}`} size="xs">
              Source email
            </Anchor>
          ) : null}
        </Group>
        {canEdit && task.status === 'open' ? (
          <Group gap="xs">
            <Button
              size="xs"
              color="green"
              variant="light"
              leftSection={<IconCheck size={14} aria-hidden />}
              loading={update.isPending && update.variables === 'done'}
              disabled={update.isPending}
              onClick={() => update.mutate('done')}
            >
              Mark done
            </Button>
            <Button
              size="xs"
              color="gray"
              variant="subtle"
              leftSection={<IconBan size={14} aria-hidden />}
              loading={update.isPending && update.variables === 'dismissed'}
              disabled={update.isPending}
              onClick={() => update.mutate('dismissed')}
            >
              Dismiss
            </Button>
          </Group>
        ) : canEdit ? (
          <Button
            size="xs"
            variant="subtle"
            w="fit-content"
            loading={update.isPending}
            onClick={() => update.mutate('open')}
          >
            Reopen
          </Button>
        ) : null}
      </Stack>
    </Card>
  );
}

function TasksTab() {
  const [params, setParams] = useSearchParams();
  const statusParam = params.get('task_status');
  const status: TaskStatus | 'all' = (TASK_STATUSES as readonly string[]).includes(statusParam ?? '')
    ? (statusParam as TaskStatus)
    : statusParam === 'all'
      ? 'all'
      : 'open';
  const offset = Math.max(0, Number(params.get('offset') ?? 0) || 0);
  const query = { status: status === 'all' ? undefined : status, limit: PAGE_SIZE, offset };
  const tasks = useQuery({
    queryKey: queryKeys.taskList(query),
    queryFn: () => tasksApi.list(query),
    placeholderData: keepPreviousData,
  });
  const set = (key: string, value: string | null) =>
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
    <Stack gap="md">
      <SegmentedControl
        w="fit-content"
        value={status}
        onChange={(value) => set('task_status', value === 'open' ? null : value)}
        data={[
          ...TASK_STATUSES.map((s) => ({ value: s, label: TASK_STATUS[s].label })),
          { value: 'all', label: 'All' },
        ]}
        aria-label="Task status"
      />
      {tasks.isPending ? (
        <SimpleGrid
          cols={{ base: 1, md: 2, xl: 3 }}
          role="status"
          aria-busy="true"
          aria-label="Loading tasks"
        >
          {Array.from({ length: 3 }, (_, i) => (
            <Skeleton key={i} height={160} />
          ))}
        </SimpleGrid>
      ) : tasks.isError ? (
        <ErrorState error={tasks.error} onRetry={() => void tasks.refetch()} retrying={tasks.isFetching} />
      ) : tasks.data.total === 0 ? (
        <EmptyPanel
          icon={<IconChecklist size={28} aria-hidden />}
          title={status === 'open' ? 'No open follow-up tasks' : 'No tasks here'}
          description="Follow-up tasks are created when an approved “follow-up task” action is executed."
        />
      ) : (
        <>
          <SimpleGrid cols={{ base: 1, md: 2, xl: 3 }} spacing="md">
            {tasks.data.items.map((task) => (
              <TaskCard key={task.id} task={task} />
            ))}
          </SimpleGrid>
          <TablePagination
            total={tasks.data.total}
            limit={tasks.data.limit || PAGE_SIZE}
            offset={tasks.data.offset}
            onOffsetChange={(next) => set('offset', next ? String(next) : null)}
          />
        </>
      )}
    </Stack>
  );
}

function DraftCard({ draft }: { draft: ReplyDraftOut }) {
  const full = `To: ${draft.to_recipients.join(', ')}\nSubject: ${draft.subject}\n\n${draft.body}`;
  return (
    <Card padding="md" component="article" aria-labelledby={`draft-${draft.id}`}>
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" wrap="wrap" gap="xs">
          <Stack gap={2} style={{ minWidth: 0, flex: 1 }}>
            <Title order={3} size="h4" id={`draft-${draft.id}`} style={{ overflowWrap: 'anywhere' }}>
              {draft.subject}
            </Title>
            <Text size="xs" c="dimmed" style={{ overflowWrap: 'anywhere' }}>
              To {draft.to_recipients.join(', ') || '—'} · <TimeText value={draft.created_at} size="xs" />
            </Text>
          </Stack>
          <Group gap="xs">
            <CopyButton value={draft.body}>
              {({ copied, copy }) => (
                <Tooltip label={copied ? 'Copied' : 'Copy the body to the clipboard'}>
                  <Button
                    size="xs"
                    variant="light"
                    color={copied ? 'green' : undefined}
                    leftSection={
                      copied ? <IconCheck size={14} aria-hidden /> : <IconCopy size={14} aria-hidden />
                    }
                    onClick={copy}
                  >
                    {copied ? 'Copied' : 'Copy body'}
                  </Button>
                </Tooltip>
              )}
            </CopyButton>
            <CopyButton value={full}>
              {({ copied, copy }) => (
                <Button
                  size="xs"
                  variant="subtle"
                  onClick={copy}
                  aria-label="Copy recipient, subject and body"
                >
                  {copied ? 'Copied' : 'Copy all'}
                </Button>
              )}
            </CopyButton>
          </Group>
        </Group>
        <Card withBorder padding="sm" bg="var(--mantine-color-body)">
          <PlainText>{draft.body}</PlainText>
        </Card>
        <Group gap="xs">
          {draft.action_id ? (
            <Anchor component={Link} to={`/actions/${draft.action_id}`} size="xs">
              Source action
            </Anchor>
          ) : null}
          {draft.email_message_id ? (
            <Anchor component={Link} to={`/emails/${draft.email_message_id}`} size="xs">
              Original email
            </Anchor>
          ) : null}
        </Group>
      </Stack>
    </Card>
  );
}

function DraftsTab() {
  const [params, setParams] = useSearchParams();
  const offset = Math.max(0, Number(params.get('draft_offset') ?? 0) || 0);
  const query = { limit: PAGE_SIZE, offset };
  const drafts = useQuery({
    queryKey: queryKeys.replyDrafts(query),
    queryFn: () => replyDraftsApi.list(query),
    placeholderData: keepPreviousData,
  });
  return (
    <Stack gap="md">
      <Alert
        color="blue"
        variant="light"
        icon={<IconMailOff size={18} aria-hidden />}
        title="Drafts are never sent by NUVEXA"
      >
        Approved replies are stored here as drafts. Copy a draft into your mail client, review it, and send it
        yourself.
      </Alert>
      {drafts.isPending ? (
        <Stack role="status" aria-busy="true" aria-label="Loading reply drafts">
          <Skeleton height={200} />
        </Stack>
      ) : drafts.isError ? (
        <ErrorState error={drafts.error} onRetry={() => void drafts.refetch()} retrying={drafts.isFetching} />
      ) : drafts.data.total === 0 ? (
        <EmptyPanel
          icon={<IconMailForward size={28} aria-hidden />}
          title="No reply drafts yet"
          description="Drafts appear here after an approved “reply draft” action is executed."
        />
      ) : (
        <>
          <Stack gap="md">
            {drafts.data.items.map((draft) => (
              <DraftCard key={draft.id} draft={draft} />
            ))}
          </Stack>
          <TablePagination
            total={drafts.data.total}
            limit={drafts.data.limit || PAGE_SIZE}
            offset={drafts.data.offset}
            onOffsetChange={(next) =>
              setParams(
                (prev) => {
                  const p = new URLSearchParams(prev);
                  if (next) p.set('draft_offset', String(next));
                  else p.delete('draft_offset');
                  return p;
                },
                { replace: true },
              )
            }
          />
        </>
      )}
    </Stack>
  );
}

export function TasksPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') === 'drafts' ? 'drafts' : 'tasks';
  return (
    <>
      <PageHeader
        title="Tasks & drafts"
        description="Internal follow-up tasks and reply drafts produced by approved actions."
      />
      <Tabs
        value={tab}
        onChange={(value) => setParams(value === 'drafts' ? { tab: 'drafts' } : {}, { replace: true })}
        keepMounted={false}
      >
        <Tabs.List mb="md">
          <Tabs.Tab value="tasks" leftSection={<IconChecklist size={16} aria-hidden />}>
            Follow-up tasks
          </Tabs.Tab>
          <Tabs.Tab value="drafts" leftSection={<IconMailForward size={16} aria-hidden />}>
            Reply drafts
          </Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="tasks">
          <TasksTab />
        </Tabs.Panel>
        <Tabs.Panel value="drafts">
          <DraftsTab />
        </Tabs.Panel>
      </Tabs>
    </>
  );
}
