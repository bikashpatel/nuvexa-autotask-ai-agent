import { describe, expect, it } from 'vitest';

import { NAV_ITEMS, isNavItemActive, type NavItem } from './navigation';

function item(to: string): NavItem {
  const found = NAV_ITEMS.find((n) => n.to === to);
  if (!found) throw new Error(`no nav item ${to}`);
  return found;
}

describe('NAV_ITEMS', () => {
  it('lists every top-level page once, in order, with a label, icon and description', () => {
    expect(NAV_ITEMS.map((n) => [n.to, n.label])).toEqual([
      ['/', 'Overview'],
      ['/inbox', 'Inbox'],
      ['/approvals', 'Approvals'],
      ['/workflows', 'Workflows'],
      ['/tasks', 'Tasks & drafts'],
      ['/integrations', 'Integrations'],
      ['/settings', 'Settings'],
      ['/audit', 'Audit log'],
    ]);
    expect(new Set(NAV_ITEMS.map((n) => n.to)).size).toBe(NAV_ITEMS.length);
    for (const nav of NAV_ITEMS) {
      expect(nav.description.trim()).not.toBe('');
      expect(nav.icon).toBeTruthy();
    }
  });
});

describe('isNavItemActive', () => {
  it('Overview is active only on the exact root', () => {
    expect(isNavItemActive(item('/'), '/')).toBe(true);
    expect(isNavItemActive(item('/'), '/inbox')).toBe(false);
    expect(isNavItemActive(item('/'), '/anything/else')).toBe(false);
  });

  it('matches the exact path and nested paths', () => {
    expect(isNavItemActive(item('/workflows'), '/workflows')).toBe(true);
    expect(isNavItemActive(item('/workflows'), '/workflows/run-1')).toBe(true);
    expect(isNavItemActive(item('/audit'), '/audit')).toBe(true);
  });

  it('matches the extra prefixes (email detail → Inbox, action review → Approvals)', () => {
    expect(isNavItemActive(item('/inbox'), '/emails/email-1')).toBe(true);
    expect(isNavItemActive(item('/approvals'), '/actions/act-1')).toBe(true);
    expect(isNavItemActive(item('/approvals'), '/actions')).toBe(true);
    expect(isNavItemActive(item('/workflows'), '/emails/email-1')).toBe(false);
  });

  it('does not match a different page that merely shares a prefix', () => {
    expect(isNavItemActive(item('/inbox'), '/inboxes')).toBe(false);
    expect(isNavItemActive(item('/inbox'), '/emailsx/1')).toBe(false);
    expect(isNavItemActive(item('/approvals'), '/actionsfoo')).toBe(false);
    expect(isNavItemActive(item('/settings'), '/settings-old')).toBe(false);
  });

  it('marks exactly one item active for every app route', () => {
    for (const pathname of [
      '/',
      '/inbox',
      '/emails/e1',
      '/approvals',
      '/actions/a1',
      '/workflows',
      '/workflows/w1',
      '/tasks',
      '/integrations',
      '/settings',
      '/audit',
    ]) {
      expect(NAV_ITEMS.filter((n) => isNavItemActive(n, pathname))).toHaveLength(1);
    }
    expect(NAV_ITEMS.filter((n) => isNavItemActive(n, '/does-not-exist'))).toHaveLength(0);
  });
});
