import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { FollowUpTaskOut, Page, ReplyDraftOut, TaskStatus } from '../../api/types';
import { expectNoSeriousA11yViolations } from '../../test/axe';
import { PERSONAS, XSS_TEXT, page, replyDraftFixture, taskFixture } from '../../test/fixtures';
import {
  callsTo,
  deferred,
  errorResponse,
  jsonResponse,
  mockFetch,
  queryOf,
  renderWithProviders,
  textContent,
  type RecordedCall,
  type RenderOptions,
  type Route,
} from '../../test/utils';
import { TasksPage } from './TasksPage';

const TASKS = '/api/v1/tasks';
const DRAFTS = '/api/v1/reply-drafts';

type Reply = Response | Promise<Response>;
type Source<T> = (call: RecordedCall) => Page<T> | Reply;

function reply<T>(source: Source<T>, call: RecordedCall): Reply {
  const result = source(call);
  return result instanceof Response || result instanceof Promise ? result : jsonResponse(result);
}

function setup({
  tasks = () => page([taskFixture()]),
  drafts = () => page([replyDraftFixture()]),
  extra = [],
  ...options
}: {
  tasks?: Source<FollowUpTaskOut>;
  drafts?: Source<ReplyDraftOut>;
  extra?: Route[];
} & RenderOptions = {}) {
  const mock = mockFetch([
    { method: 'GET', path: TASKS, handler: (call) => reply(tasks, call) },
    { method: 'GET', path: DRAFTS, handler: (call) => reply(drafts, call) },
    ...extra,
  ]);
  return { ...mock, ...renderWithProviders(<TasksPage />, options) };
}

const lastParams = (calls: RecordedCall[], path: string) => queryOf(callsTo(calls, 'GET', path).at(-1)!);

function card(title: string) {
  return within(screen.getByRole('article', { name: title }));
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('<TasksPage> tabs', () => {
  it('shows follow-up tasks by default and switches to reply drafts via the URL', async () => {
    const user = userEvent.setup();
    const { router, calls } = setup();
    expect(screen.getByRole('heading', { level: 1, name: 'Tasks & drafts' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Follow-up tasks' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByRole('article', { name: 'Call Dana back' })).toBeInTheDocument();
    expect(callsTo(calls, 'GET', DRAFTS)).toHaveLength(0);

    await user.click(screen.getByRole('tab', { name: 'Reply drafts' }));
    await waitFor(() => expect(router.state.location.search).toBe('?tab=drafts'));
    expect(await screen.findByRole('article', { name: 'Re: URGENT: checkout broken' })).toBeInTheDocument();
    expect(screen.queryByRole('article', { name: 'Call Dana back' })).toBeNull();

    await user.click(screen.getByRole('tab', { name: 'Follow-up tasks' }));
    await waitFor(() => expect(router.state.location.search).toBe(''));
  });

  it('opens the drafts tab straight from the URL', async () => {
    setup({ route: '/?tab=drafts' });
    expect(screen.getByRole('tab', { name: 'Reply drafts' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByText('Drafts are never sent by NUVEXA')).toBeInTheDocument();
  });
});

describe('<TasksPage> follow-up tasks', () => {
  it('shows a busy skeleton while loading', () => {
    const hold = deferred<Response>();
    setup({ tasks: () => hold.promise });
    expect(screen.getByLabelText('Loading tasks')).toHaveAttribute('aria-busy', 'true');
  });

  it('shows the error with its correlation id and retries', async () => {
    const user = userEvent.setup();
    let fail = true;
    const { calls } = setup({
      tasks: () =>
        fail ? errorResponse(500, 'internal_error', { correlationId: 'corr-tasks' }) : page([taskFixture()]),
    });
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('corr-tasks')).toBeInTheDocument();
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('article', { name: 'Call Dana back' })).toBeInTheDocument();
    expect(callsTo(calls, 'GET', TASKS)).toHaveLength(2);
  });

  it('has status-specific empty copy', async () => {
    const { unmount } = setup({ tasks: () => page([]) });
    expect(await screen.findByText('No open follow-up tasks')).toBeInTheDocument();
    unmount();
    setup({ tasks: () => page([]), route: '/?task_status=done' });
    expect(await screen.findByText('No tasks here')).toBeInTheDocument();
  });

  it('filters by status (open by default, "all" sends no status) and ignores invalid values', async () => {
    const user = userEvent.setup();
    const { calls, router } = setup({ route: '/?task_status=bogus' });
    await screen.findByRole('article', { name: 'Call Dana back' });
    expect(lastParams(calls, TASKS).get('status')).toBe('open');
    expect(screen.getByRole('radiogroup', { name: 'Task status' })).toBeInTheDocument();

    await user.click(screen.getByRole('radio', { name: 'Dismissed' }));
    await waitFor(() => expect(router.state.location.search).toBe('?task_status=dismissed'));
    await waitFor(() => expect(lastParams(calls, TASKS).get('status')).toBe('dismissed'));

    await user.click(screen.getByRole('radio', { name: 'All' }));
    await waitFor(() => expect(router.state.location.search).toBe('?task_status=all'));
    await waitFor(() => expect(lastParams(calls, TASKS).has('status')).toBe(false));

    await user.click(screen.getByRole('radio', { name: 'Open' }));
    await waitFor(() => expect(router.state.location.search).toBe(''));
  });

  it('renders cards with an overdue badge, description, source links and named controls', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-25T12:00:00Z'));
    const { container } = setup({
      tasks: () =>
        page([
          taskFixture(),
          taskFixture({
            id: 'task-2',
            title: 'Send the RCA',
            description: '',
            due_at: '2026-11-30T09:00:00Z',
            action_id: null,
            email_message_id: null,
          }),
          taskFixture({ id: 'task-3', title: 'No deadline', due_at: null }),
        ]),
    });
    const overdue = await screen.findByRole('article', { name: 'Call Dana back' });
    expect(within(overdue).getByText(/^Overdue · Oct 20, 2026/)).toBeInTheDocument();
    expect(within(overdue).getByText('Confirm the fix with the customer.')).toBeInTheDocument();
    expect(within(overdue).getByLabelText('Status: Open')).toBeInTheDocument();
    expect(within(overdue).getByRole('link', { name: 'Source action' })).toHaveAttribute(
      'href',
      '/actions/act-follow',
    );
    expect(within(overdue).getByRole('link', { name: 'Source email' })).toHaveAttribute(
      'href',
      '/emails/email-1',
    );
    expect(within(overdue).getByRole('button', { name: 'Mark done' })).toBeEnabled();
    expect(within(overdue).getByRole('button', { name: 'Dismiss' })).toBeEnabled();

    const future = card('Send the RCA');
    expect(future.getByText(/^Due Nov 30, 2026/)).toBeInTheDocument();
    expect(future.queryByRole('link')).toBeNull();

    expect(card('No deadline').getByText('No due date')).toBeInTheDocument();
    vi.useRealTimers();
    await expectNoSeriousA11yViolations(container);
  });

  it('does not flag a completed task as overdue and offers Reopen', async () => {
    setup({ tasks: () => page([taskFixture({ status: 'done', completed_at: '2026-10-21T09:00:00Z' })]) });
    const done = await screen.findByRole('article', { name: 'Call Dana back' });
    expect(within(done).getByText(/^Due Oct 20, 2026/)).toBeInTheDocument();
    expect(within(done).getByLabelText('Status: Done')).toBeInTheDocument();
    expect(within(done).getByRole('button', { name: 'Reopen' })).toBeInTheDocument();
    expect(within(done).queryByRole('button', { name: 'Mark done' })).toBeNull();
  });

  it('shows no mutating controls to a viewer', async () => {
    setup({
      session: PERSONAS.viewer,
      tasks: () => page([taskFixture(), taskFixture({ id: 'task-2', title: 'Old', status: 'dismissed' })]),
    });
    await screen.findByRole('article', { name: 'Call Dana back' });
    expect(screen.queryByRole('button', { name: /Mark done|Dismiss|Reopen/ })).toBeNull();
  });

  it('updates status only after the server confirms (no optimistic change, one request)', async () => {
    const user = userEvent.setup();
    const hold = deferred<Response>();
    let status: TaskStatus = 'open';
    const { calls } = setup({
      withNotifications: true,
      tasks: () => page([taskFixture({ status })]),
      extra: [{ method: 'PATCH', path: '/api/v1/tasks/task-1', handler: () => hold.promise }],
    });
    const article = await screen.findByRole('article', { name: 'Call Dana back' });
    const markDone = within(article).getByRole('button', { name: 'Mark done' });
    await user.click(markDone);
    await user.click(markDone);

    await waitFor(() => expect(markDone).toHaveAttribute('data-loading', 'true'));
    expect(markDone).toBeDisabled();
    expect(within(article).getByRole('button', { name: 'Dismiss' })).toBeDisabled();
    expect(within(article).getByLabelText('Status: Open')).toBeInTheDocument();
    const patches = callsTo(calls, 'PATCH', '/api/v1/tasks/task-1');
    expect(patches).toHaveLength(1);
    expect(patches[0]!.body).toEqual({ status: 'done' });

    const listBefore = callsTo(calls, 'GET', TASKS).length;
    status = 'done';
    hold.resolve(jsonResponse(taskFixture({ status: 'done' })));
    expect(await screen.findByText('Task marked done')).toBeInTheDocument();
    await waitFor(() => expect(callsTo(calls, 'GET', TASKS).length).toBeGreaterThan(listBefore));
    expect(await screen.findByLabelText('Status: Done')).toBeInTheDocument();
  });

  it.each([
    ['Dismiss', 'open', 'dismissed', 'Task dismissed'],
    ['Reopen', 'dismissed', 'open', 'Task reopened'],
  ] as const)('%s sends PATCH {status: %s → %s} and toasts', async (button, from, to, toast) => {
    const user = userEvent.setup();
    const { calls } = setup({
      withNotifications: true,
      tasks: () => page([taskFixture({ status: from })]),
      extra: [
        {
          method: 'PATCH',
          path: '/api/v1/tasks/task-1',
          handler: () => jsonResponse(taskFixture({ status: to })),
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: button }));
    expect(await screen.findByText(toast)).toBeInTheDocument();
    expect(callsTo(calls, 'PATCH', '/api/v1/tasks/task-1')[0]!.body).toEqual({ status: to });
  });

  it('toasts a failed update with its support reference and keeps the card unchanged', async () => {
    const user = userEvent.setup();
    setup({
      withNotifications: true,
      extra: [
        {
          method: 'PATCH',
          path: '/api/v1/tasks/task-1',
          handler: () =>
            errorResponse(403, 'permission_denied', {
              message: 'Not your task.',
              correlationId: 'corr-task',
            }),
        },
      ],
    });
    await user.click(await screen.findByRole('button', { name: 'Mark done' }));
    expect(await screen.findByText('Could not update the task')).toBeInTheDocument();
    expect(screen.getByText('Not your task.')).toBeInTheDocument();
    expect(screen.getByText('permission_denied · ref corr-task')).toBeInTheDocument();
    expect(card('Call Dana back').getByLabelText('Status: Open')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Mark done' })).toBeEnabled();
  });

  it('paginates tasks with offset', async () => {
    const user = userEvent.setup();
    const { calls, router } = setup({
      tasks: (call) => page([taskFixture()], { total: 40, offset: Number(queryOf(call).get('offset')) }),
    });
    await screen.findByText('1–25 of 40');
    await user.click(screen.getByRole('button', { name: 'Page 2' }));
    await waitFor(() => expect(router.state.location.search).toBe('?offset=25'));
    expect(await screen.findByText('26–40 of 40')).toBeInTheDocument();
    expect(lastParams(calls, TASKS).get('offset')).toBe('25');
    await user.click(screen.getByRole('button', { name: 'Page 1' }));
    await waitFor(() => expect(router.state.location.search).toBe(''));
  });
});

describe('<TasksPage> reply drafts', () => {
  it('shows a busy skeleton, then the error with retry', async () => {
    const user = userEvent.setup();
    const hold = deferred<Response>();
    let source: Source<ReplyDraftOut> = () => hold.promise;
    const { calls } = setup({ route: '/?tab=drafts', drafts: (call) => source(call) });
    expect(screen.getByLabelText('Loading reply drafts')).toHaveAttribute('aria-busy', 'true');
    hold.resolve(errorResponse(500, 'internal_error', { correlationId: 'corr-drafts' }));
    // (The "never sent" notice is also an Alert, so locate the error panel by its reference.)
    const alert = (await screen.findByText('corr-drafts')).closest<HTMLElement>('[role="alert"]')!;
    expect(alert).toHaveTextContent('Something went wrong');
    source = () => page([replyDraftFixture()]);
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('article', { name: 'Re: URGENT: checkout broken' })).toBeInTheDocument();
    expect(callsTo(calls, 'GET', DRAFTS)).toHaveLength(2);
  });

  it('has an empty state', async () => {
    setup({ route: '/?tab=drafts', drafts: () => page([]) });
    expect(await screen.findByText('No reply drafts yet')).toBeInTheDocument();
  });

  it('renders the draft body as plain text with links to its sources', async () => {
    const { container } = setup({
      route: '/?tab=drafts',
      drafts: () =>
        page([
          replyDraftFixture({ body: `Hello Dana,\n${XSS_TEXT}` }),
          replyDraftFixture({
            id: 'draft-2',
            subject: 'Re: invoice',
            to_recipients: [],
            action_id: null,
            email_message_id: null,
          }),
        ]),
    });
    const draft = within(await screen.findByRole('article', { name: 'Re: URGENT: checkout broken' }));
    expect(draft.getByText(textContent(/^To dana@fabrikam\.example ·/))).toBeInTheDocument();
    expect(draft.getByText(/Hello Dana,/)).toHaveTextContent(XSS_TEXT);
    expect(container.querySelector('article img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(draft.getByRole('link', { name: 'Source action' })).toHaveAttribute('href', '/actions/act-reply');
    expect(draft.getByRole('link', { name: 'Original email' })).toHaveAttribute('href', '/emails/email-1');

    const second = card('Re: invoice');
    expect(second.getByText(/^To — ·/)).toBeInTheDocument();
    expect(second.queryByRole('link')).toBeNull();
    await expectNoSeriousA11yViolations(container);
  });

  it('copies the body, and recipient + subject + body', async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, 'writeText');
    setup({ route: '/?tab=drafts' });
    const draft = within(await screen.findByRole('article', { name: 'Re: URGENT: checkout broken' }));

    await user.click(draft.getByRole('button', { name: 'Copy body' }));
    expect(writeText).toHaveBeenLastCalledWith('Hi Dana,\n\nWe deployed a fix at 10:30 UTC.\n\nRegards');
    expect(await draft.findByRole('button', { name: 'Copied' })).toBeInTheDocument();

    await user.click(draft.getByRole('button', { name: 'Copy recipient, subject and body' }));
    expect(writeText).toHaveBeenLastCalledWith(
      'To: dana@fabrikam.example\nSubject: Re: URGENT: checkout broken\n\nHi Dana,\n\nWe deployed a fix at 10:30 UTC.\n\nRegards',
    );
  });

  it('paginates drafts with draft_offset', async () => {
    const user = userEvent.setup();
    const { calls, router } = setup({
      route: '/?tab=drafts',
      drafts: (call) =>
        page([replyDraftFixture()], { total: 30, offset: Number(queryOf(call).get('offset')) }),
    });
    await screen.findByText('1–25 of 30');
    await user.click(screen.getByRole('button', { name: 'Page 2' }));
    await waitFor(() => expect(router.state.location.search).toBe('?tab=drafts&draft_offset=25'));
    expect(await screen.findByText('26–30 of 30')).toBeInTheDocument();
    expect(lastParams(calls, DRAFTS).get('offset')).toBe('25');
    await user.click(screen.getByRole('button', { name: 'Page 1' }));
    await waitFor(() => expect(router.state.location.search).toBe('?tab=drafts'));
  });
});
