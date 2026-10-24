import {
  IconChecklist,
  IconClipboardCheck,
  IconGauge,
  IconHistory,
  IconInbox,
  IconPlugConnected,
  IconRoute,
  IconSettings,
} from '@tabler/icons-react';

export interface NavItem {
  to: string;
  label: string;
  icon: typeof IconInbox;
  /** Additional path prefixes that should mark this item active. */
  match?: string[];
  description: string;
}

export const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Overview', icon: IconGauge, description: 'KPIs and recent activity' },
  { to: '/inbox', label: 'Inbox', icon: IconInbox, match: ['/emails'], description: 'Synced email' },
  {
    to: '/approvals',
    label: 'Approvals',
    icon: IconClipboardCheck,
    match: ['/actions'],
    description: 'Actions awaiting a decision',
  },
  { to: '/workflows', label: 'Workflows', icon: IconRoute, description: 'Analysis runs' },
  { to: '/tasks', label: 'Tasks & drafts', icon: IconChecklist, description: 'Follow-ups and reply drafts' },
  {
    to: '/integrations',
    label: 'Integrations',
    icon: IconPlugConnected,
    description: 'Microsoft 365, Azure DevOps, LLM',
  },
  { to: '/settings', label: 'Settings', icon: IconSettings, description: 'Tenant configuration' },
  { to: '/audit', label: 'Audit log', icon: IconHistory, description: 'Who did what, when' },
];

export function isNavItemActive(item: NavItem, pathname: string): boolean {
  if (item.to === '/') return pathname === '/';
  const prefixes = [item.to, ...(item.match ?? [])];
  return prefixes.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}
