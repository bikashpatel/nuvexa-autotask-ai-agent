import {
  Alert,
  Badge,
  Box,
  Button,
  Card,
  Divider,
  Group,
  Loader,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  ThemeIcon,
  Title,
  UnstyledButton,
} from '@mantine/core';
import {
  IconBrandWindows,
  IconChecklist,
  IconEye,
  IconFingerprint,
  IconLock,
  IconShieldCheck,
  IconShieldLock,
  IconTimeline,
  IconUserCheck,
  IconUserCog,
  IconUser,
} from '@tabler/icons-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Navigate, useNavigate, useSearchParams } from 'react-router';

import { navigationUrl, safeRedirectPath } from '../../api/client';
import { authApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import type { DemoPersona } from '../../api/types';
import { ErrorState } from '../../components/ErrorState';
import { BrandMark } from '../shell/BrandMark';
import { useAuthConfig, useMeQuery } from './session';
import { notifyError } from '../../lib/notify';

const PERSONAS: {
  persona: DemoPersona;
  title: string;
  description: string;
  icon: typeof IconUser;
}[] = [
  {
    persona: 'admin',
    title: 'Admin',
    description: 'Everything an approver can do, plus tenant settings and the full audit log.',
    icon: IconUserCog,
  },
  {
    persona: 'approver',
    title: 'Approver',
    description: 'Review, approve, reject and execute actions across the tenant.',
    icon: IconUserCheck,
  },
  {
    persona: 'member',
    title: 'Member',
    description: 'Sync your mailbox, analyze email and approve your own actions.',
    icon: IconUser,
  },
  {
    persona: 'viewer',
    title: 'Viewer',
    description: 'Read-only access to your own emails and actions.',
    icon: IconEye,
  },
];

const VALUE_PROPS = [
  {
    icon: IconShieldCheck,
    title: 'Human-approved, always',
    text: 'The AI proposes. Nothing is written to Azure DevOps until a person approves the exact payload.',
  },
  {
    icon: IconFingerprint,
    title: 'Approval bound to the payload',
    text: 'Every approval is tied to a version and SHA-256 payload hash. Any edit invalidates it.',
  },
  {
    icon: IconShieldLock,
    title: 'Prompt-injection screening',
    text: 'Email is treated as untrusted data. Suspicious instructions are flagged for the reviewer.',
  },
  {
    icon: IconTimeline,
    title: 'Fully auditable',
    text: 'Every step, model call, approval and external write is traced and recorded.',
  },
];

export function LoginPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const redirectTo = safeRedirectPath(params.get('redirect_to'));
  const config = useAuthConfig();
  const me = useMeQuery();

  const demoLogin = useMutation({
    mutationFn: authApi.demoLogin,
    onSuccess: (session) => {
      queryClient.clear();
      queryClient.setQueryData(queryKeys.me, session);
      void navigate(redirectTo, { replace: true });
    },
    onError: (error) => notifyError('Demo sign-in failed', error),
  });

  if (me.isSuccess) return <Navigate to={redirectTo} replace />;

  const signInError = params.get('error');

  return (
    <Box
      mih="100vh"
      style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 420px), 1fr))' }}
    >
      <Box
        className="nuvexa-hero"
        p={{ base: 'lg', sm: 48 }}
        component="section"
        aria-label="About NUVEXA AutoTask"
      >
        <Stack gap="xl" maw={560} mx="auto" h="100%" justify="center">
          <Group gap="sm">
            <BrandMark size={44} />
            <div>
              <Text fw={700} size="xl" lh={1.1} c="white">
                NUVEXA AutoTask
              </Text>
              <Text size="sm" c="gray.4">
                by Elarxsys
              </Text>
            </div>
          </Group>
          <Stack gap="sm">
            <Badge variant="outline" color="lagoon.3" size="lg" w="fit-content">
              Enterprise AI operations
            </Badge>
            <Title order={1} c="white" fz={{ base: 30, sm: 40 }} lh={1.15}>
              Turn incoming email into reviewed, approved work.
            </Title>
            <Text c="gray.3" size="lg">
              NUVEXA classifies requests, extracts the facts and drafts Azure DevOps work items, replies and
              follow-ups — then waits for a human to approve every change.
            </Text>
          </Stack>
          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg">
            {VALUE_PROPS.map((item) => (
              <Group key={item.title} align="flex-start" gap="sm" wrap="nowrap">
                <ThemeIcon variant="light" color="lagoon" size={36} radius="md" aria-hidden>
                  <item.icon size={20} />
                </ThemeIcon>
                <div>
                  <Text fw={600} c="white" size="sm">
                    {item.title}
                  </Text>
                  <Text size="sm" c="gray.4">
                    {item.text}
                  </Text>
                </div>
              </Group>
            ))}
          </SimpleGrid>
        </Stack>
      </Box>

      <Box component="main" p={{ base: 'lg', sm: 48 }} style={{ display: 'flex', alignItems: 'center' }}>
        <Stack gap="lg" maw={480} w="100%" mx="auto">
          <div>
            <Title order={2}>Sign in</Title>
            <Text c="dimmed" size="sm">
              Use your organization&apos;s Microsoft account.
            </Text>
          </div>

          {signInError ? (
            <Alert
              color="red"
              variant="light"
              title="Sign-in did not complete"
              icon={<IconLock size={18} aria-hidden />}
            >
              Microsoft sign-in was cancelled or failed. Try again, or contact your administrator.
            </Alert>
          ) : null}

          {config.isPending ? (
            <Stack gap="sm" role="status" aria-busy="true" aria-label="Loading sign-in options">
              <Skeleton height={44} />
              <Skeleton height={120} />
            </Stack>
          ) : config.isError ? (
            <ErrorState
              error={config.error}
              title="Could not load sign-in options"
              onRetry={() => void config.refetch()}
              retrying={config.isFetching}
            />
          ) : (
            <>
              {config.data.entra_login_enabled ? (
                <Button
                  component="a"
                  href={navigationUrl('/auth/login', redirectTo)}
                  size="md"
                  leftSection={<IconBrandWindows size={20} aria-hidden />}
                  fullWidth
                >
                  Sign in with Microsoft
                </Button>
              ) : (
                <Stack gap={6}>
                  <Button
                    size="md"
                    disabled
                    fullWidth
                    leftSection={<IconBrandWindows size={20} aria-hidden />}
                    aria-describedby="entra-disabled-reason"
                  >
                    Sign in with Microsoft
                  </Button>
                  <Text size="xs" c="dimmed" id="entra-disabled-reason">
                    Microsoft Entra ID sign-in is not configured for this environment (
                    {config.data.environment}). An administrator must set the Entra client registration on the
                    API.
                  </Text>
                </Stack>
              )}

              {config.data.demo_login_enabled ? (
                <Stack gap="sm">
                  <Divider
                    label={
                      <Group gap={6}>
                        <IconChecklist size={14} aria-hidden />
                        <span>Demo mode — choose a persona</span>
                      </Group>
                    }
                    labelPosition="center"
                  />
                  <Text size="xs" c="dimmed">
                    Demo personas use a synthetic mailbox, the heuristic analysis agent and a mock Azure
                    DevOps. No external system is contacted.
                  </Text>
                  <SimpleGrid cols={{ base: 1, xs: 2 }} spacing="sm">
                    {PERSONAS.map((p) => {
                      const loading = demoLogin.isPending && demoLogin.variables === p.persona;
                      return (
                        <UnstyledButton
                          key={p.persona}
                          onClick={() => demoLogin.mutate(p.persona)}
                          disabled={demoLogin.isPending}
                          aria-label={`Sign in as demo ${p.title}`}
                          style={{ borderRadius: 'var(--mantine-radius-md)' }}
                        >
                          <Card
                            padding="md"
                            h="100%"
                            style={{ cursor: demoLogin.isPending ? 'wait' : 'pointer' }}
                          >
                            <Group gap="sm" wrap="nowrap" align="flex-start">
                              <ThemeIcon variant="light" size={34} radius="md" aria-hidden>
                                {loading ? <Loader size={16} /> : <p.icon size={18} />}
                              </ThemeIcon>
                              <div>
                                <Text fw={600} size="sm">
                                  {p.title}
                                </Text>
                                <Text size="xs" c="dimmed">
                                  {p.description}
                                </Text>
                              </div>
                            </Group>
                          </Card>
                        </UnstyledButton>
                      );
                    })}
                  </SimpleGrid>
                </Stack>
              ) : null}
              <Text size="xs" c="dimmed">
                {config.data.app_name} · {config.data.environment}
                {config.data.demo_mode ? ' · demo mode' : ''}
              </Text>
            </>
          )}
        </Stack>
      </Box>
    </Box>
  );
}
