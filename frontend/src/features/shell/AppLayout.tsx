import {
  AppShell,
  Avatar,
  Badge,
  Burger,
  Group,
  Menu,
  NavLink,
  ScrollArea,
  Stack,
  Text,
  Tooltip,
  UnstyledButton,
} from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import { IconChevronDown, IconFlask, IconLogout } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Suspense } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router';

import { actionsApi, authApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import { humanize } from '../../lib/format';
import { useAuthConfig, useSession } from '../auth/session';
import { DetailSkeleton } from '../../components/LoadingSkeleton';
import { BrandMark } from './BrandMark';
import { ColorSchemeToggle } from './ColorSchemeToggle';
import { NAV_ITEMS, isNavItemActive } from './navigation';

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  return (parts[0]?.[0] ?? '?').toUpperCase() + (parts[1]?.[0] ?? '').toUpperCase();
}

function PendingCount() {
  const pending = useQuery({
    queryKey: queryKeys.actionList({ status: 'pending_approval', limit: 1, offset: 0 }),
    queryFn: () => actionsApi.list({ status: 'pending_approval', limit: 1, offset: 0 }),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const total = pending.data?.total ?? 0;
  if (!total) return null;
  return (
    <Badge
      size="sm"
      color="orange"
      variant="filled"
      circle={total < 10}
      aria-label={`${total} pending approvals`}
    >
      {total > 99 ? '99+' : total}
    </Badge>
  );
}

export function AppLayout() {
  const [opened, { toggle, close }] = useDisclosure(false);
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const session = useSession();
  const config = useAuthConfig();
  const demoMode = config.data?.demo_mode ?? false;

  const logout = useMutation({
    mutationFn: authApi.logout,
    onSettled: () => {
      queryClient.clear();
      void navigate('/login', { replace: true });
    },
  });

  const { user } = session;

  return (
    <AppShell
      header={{ height: 60 }}
      navbar={{ width: 248, breakpoint: 'sm', collapsed: { mobile: !opened } }}
      padding={{ base: 'md', sm: 'lg' }}
      className="nuvexa-app-bg"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap" gap="sm">
          <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
            <Burger
              opened={opened}
              onClick={toggle}
              hiddenFrom="sm"
              size="sm"
              aria-label={opened ? 'Close navigation' : 'Open navigation'}
            />
            <UnstyledButton component={Link} to="/" aria-label="NUVEXA AutoTask home">
              <Group gap="xs" wrap="nowrap">
                <BrandMark size={30} />
                <Stack gap={0} visibleFrom="xs">
                  <Text fw={700} size="sm" lh={1.1}>
                    NUVEXA AutoTask
                  </Text>
                  <Text size="xs" c="dimmed" lh={1.1}>
                    by Elarxsys
                  </Text>
                </Stack>
              </Group>
            </UnstyledButton>
            {demoMode ? (
              <Tooltip label="Demo mode: synthetic mailbox, heuristic agent and mock Azure DevOps. No external writes.">
                <Badge
                  color="grape"
                  variant="light"
                  leftSection={<IconFlask size={12} aria-hidden />}
                  aria-label="Demo mode active"
                >
                  Demo mode
                </Badge>
              </Tooltip>
            ) : null}
            {config.data && config.data.environment !== 'production' ? (
              <Badge color="gray" variant="outline" visibleFrom="md">
                {config.data.environment}
              </Badge>
            ) : null}
          </Group>

          <Group gap="sm" wrap="nowrap">
            <ColorSchemeToggle />
            <Menu position="bottom-end" width={260} withinPortal>
              <Menu.Target>
                <UnstyledButton aria-label={`Account menu for ${user.display_name}`}>
                  <Group gap="xs" wrap="nowrap">
                    <Avatar color="nuvexa" radius="xl" size={32}>
                      {initials(user.display_name)}
                    </Avatar>
                    <Stack gap={0} visibleFrom="md" style={{ minWidth: 0 }}>
                      <Text size="sm" fw={600} lh={1.2} truncate maw={180}>
                        {user.display_name}
                      </Text>
                      <Text size="xs" c="dimmed" lh={1.2} truncate maw={180}>
                        {humanize(user.role)} · {user.tenant_name}
                      </Text>
                    </Stack>
                    <IconChevronDown size={14} aria-hidden />
                  </Group>
                </UnstyledButton>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Label>Signed in as</Menu.Label>
                <Stack gap={2} px="sm" pb="xs">
                  <Text size="sm" fw={600}>
                    {user.display_name}
                  </Text>
                  <Text size="xs" c="dimmed" style={{ overflowWrap: 'anywhere' }}>
                    {user.email}
                  </Text>
                  <Group gap={6} mt={4}>
                    <Badge size="sm" variant="light">
                      {humanize(user.role)}
                    </Badge>
                    <Badge size="sm" variant="outline" color="gray">
                      {user.tenant_name}
                    </Badge>
                  </Group>
                </Stack>
                <Menu.Divider />
                <Menu.Item
                  leftSection={<IconLogout size={16} aria-hidden />}
                  color="red"
                  onClick={() => logout.mutate()}
                  disabled={logout.isPending}
                >
                  {logout.isPending ? 'Signing out…' : 'Sign out'}
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="sm" aria-label="Main navigation">
        <AppShell.Section grow component={ScrollArea}>
          <Stack gap={2} component="nav">
            {NAV_ITEMS.map((item) => {
              const active = isNavItemActive(item, location.pathname);
              return (
                <NavLink
                  key={item.to}
                  component={Link}
                  to={item.to}
                  label={item.label}
                  leftSection={<item.icon size={18} stroke={1.75} aria-hidden />}
                  rightSection={item.to === '/approvals' ? <PendingCount /> : undefined}
                  active={active}
                  onClick={close}
                  aria-current={active ? 'page' : undefined}
                  variant="light"
                  style={{ borderRadius: 'var(--mantine-radius-md)' }}
                />
              );
            })}
          </Stack>
        </AppShell.Section>
        <AppShell.Section>
          <Stack gap={2} p="xs">
            <Text size="xs" c="dimmed">
              Proposals are never executed without a human approval.
            </Text>
            <Text size="xs" c="dimmed">
              © Elarxsys
            </Text>
          </Stack>
        </AppShell.Section>
      </AppShell.Navbar>

      <AppShell.Main>
        <Suspense fallback={<DetailSkeleton />}>
          <Outlet />
        </Suspense>
      </AppShell.Main>
    </AppShell>
  );
}
