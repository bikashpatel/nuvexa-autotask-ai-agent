import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';

import { queryKeys } from './queryKeys';

function startsWith(key: readonly unknown[], prefix: readonly unknown[]): boolean {
  return prefix.every((part, index) => key[index] === part);
}

describe('queryKeys', () => {
  it('static keys have the documented shape', () => {
    expect(queryKeys.authConfig).toEqual(['auth', 'config']);
    expect(queryKeys.me).toEqual(['auth', 'me']);
    expect(queryKeys.health).toEqual(['health', 'ready']);
    expect(queryKeys.dashboard).toEqual(['dashboard', 'summary']);
    expect(queryKeys.integrations).toEqual(['integrations']);
    expect(queryKeys.adoProjects).toEqual(['integrations', 'azure-devops', 'projects']);
    expect(queryKeys.emails).toEqual(['emails']);
    expect(queryKeys.workflows).toEqual(['workflows']);
    expect(queryKeys.actions).toEqual(['actions']);
    expect(queryKeys.tasks).toEqual(['tasks']);
    expect(queryKeys.settings).toEqual(['settings']);
  });

  it('parameterised keys embed their arguments', () => {
    const filters = { q: 'x', limit: 25 };
    expect(queryKeys.adoWorkItemTypes('Payments')).toEqual([
      'integrations',
      'azure-devops',
      'projects',
      'Payments',
      'types',
    ]);
    expect(queryKeys.emailList(filters)).toEqual(['emails', 'list', filters]);
    expect(queryKeys.email('e1')).toEqual(['emails', 'detail', 'e1']);
    expect(queryKeys.workflowList(filters)).toEqual(['workflows', 'list', filters]);
    expect(queryKeys.workflow('r1')).toEqual(['workflows', 'detail', 'r1']);
    expect(queryKeys.workflowAnalysis('r1')).toEqual(['workflows', 'detail', 'r1', 'analysis']);
    expect(queryKeys.actionList(filters)).toEqual(['actions', 'list', filters]);
    expect(queryKeys.action('a1')).toEqual(['actions', 'detail', 'a1']);
    expect(queryKeys.actionHistory('a1')).toEqual(['actions', 'detail', 'a1', 'history']);
    expect(queryKeys.taskList(filters)).toEqual(['tasks', 'list', filters]);
    expect(queryKeys.replyDrafts(filters)).toEqual(['reply-drafts', filters]);
    expect(queryKeys.audit(filters)).toEqual(['audit-events', filters]);
  });

  it('nests keys under the prefixes used for invalidation', () => {
    expect(startsWith(queryKeys.action('a1'), queryKeys.actions)).toBe(true);
    expect(startsWith(queryKeys.actionList({}), ['actions', 'list'])).toBe(true);
    expect(startsWith(queryKeys.actionHistory('a1'), queryKeys.action('a1'))).toBe(true);
    expect(startsWith(queryKeys.email('e1'), queryKeys.emails)).toBe(true);
    expect(startsWith(queryKeys.emailList({}), queryKeys.emails)).toBe(true);
    expect(startsWith(queryKeys.workflow('r1'), queryKeys.workflows)).toBe(true);
    expect(startsWith(queryKeys.workflowAnalysis('r1'), queryKeys.workflow('r1'))).toBe(true);
    expect(startsWith(queryKeys.taskList({}), queryKeys.tasks)).toBe(true);
    expect(startsWith(queryKeys.adoProjects, queryKeys.integrations)).toBe(true);
    expect(startsWith(queryKeys.adoWorkItemTypes('P'), queryKeys.adoProjects)).toBe(true);
    // The session key must NOT be under any data prefix (it survives data invalidation).
    expect(startsWith(queryKeys.me, queryKeys.actions)).toBe(false);
  });

  it('invalidating ["actions","list"] marks every list stale but leaves action details fresh', async () => {
    const client = new QueryClient();
    client.setQueryData(queryKeys.actionList({ status: 'pending_approval' }), { items: [] });
    client.setQueryData(queryKeys.actionList({ status: 'approved' }), { items: [] });
    client.setQueryData(queryKeys.action('a1'), { id: 'a1' });
    await client.invalidateQueries({ queryKey: ['actions', 'list'] });
    const state = (key: readonly unknown[]) => client.getQueryState(key)?.isInvalidated;
    expect(state(queryKeys.actionList({ status: 'pending_approval' }))).toBe(true);
    expect(state(queryKeys.actionList({ status: 'approved' }))).toBe(true);
    expect(state(queryKeys.action('a1'))).toBe(false);
  });
});
