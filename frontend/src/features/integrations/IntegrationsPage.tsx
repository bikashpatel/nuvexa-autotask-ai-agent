import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Grid,
  Group,
  List,
  Select,
  Skeleton,
  Stack,
  Table,
  Text,
  ThemeIcon,
  Tooltip,
} from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import {
  IconBrain,
  IconBrandAzure,
  IconBrandOffice,
  IconPlugConnected,
  IconPlugConnectedX,
  IconRefresh,
  IconStethoscope,
  IconTestPipe,
} from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { navigationUrl } from '../../api/client';
import { healthApi, integrationsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import type { AzureDevOpsIntegrationOut, ConnectionOut, MicrosoftIntegrationOut } from '../../api/types';
import { ConfirmModal } from '../../components/ConfirmModal';
import { MockBadge } from '../../components/DomainBadges';
import { ErrorState } from '../../components/ErrorState';
import { ExternalLink } from '../../components/ExternalLink';
import { FactList } from '../../components/LabeledValue';
import { PageHeader } from '../../components/PageHeader';
import { SectionCard } from '../../components/SectionCard';
import { StatusBadge } from '../../components/StatusBadge';
import { TimeText } from '../../components/TimeText';
import { formatInteger, formatLatency, humanize } from '../../lib/format';
import { notifyError, notifySuccess } from '../../lib/notify';
import { useAuthConfig, useHasPermission, useSession } from '../auth/session';
import { useSyncMailbox } from '../inbox/hooks';

function YesNo({ value, yes = 'Yes', no = 'No' }: { value: boolean; yes?: string; no?: string }) {
  return (
    <Badge variant="light" color={value ? 'green' : 'gray'} size="sm">
      {value ? yes : no}
    </Badge>
  );
}

function ModeBadge({ mode }: { mode: string }) {
  const isMock = /mock|demo/i.test(mode);
  return (
    <Group gap={6}>
      <Badge variant="outline" color={isMock ? 'grape' : 'gray'}>
        {humanize(mode)}
      </Badge>
      {isMock ? <MockBadge /> : null}
    </Group>
  );
}

/**
 * DELETE one of the caller's own connections (the server checks ownership and returns 404 otherwise),
 * then refetch. Never optimistic: the connection stays listed until the server confirms.
 */
function useDisconnect(connectionId: string, closeConfirm: () => void, successMessage: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => integrationsApi.disconnect(connectionId),
    onSuccess: () => {
      closeConfirm();
      notifySuccess('Disconnected', successMessage);
      void queryClient.invalidateQueries({ queryKey: queryKeys.integrations });
    },
    onError: (error) => {
      closeConfirm();
      notifyError('Could not disconnect', error);
    },
  });
}

function ConnectionRow({ connection }: { connection: ConnectionOut }) {
  const canSync = useHasPermission('email:sync');
  const sync = useSyncMailbox();
  const [confirmOpen, confirm] = useDisclosure(false);
  const disconnect = useDisconnect(
    connection.id,
    confirm.close,
    'Stored tokens were deleted and sync stopped.',
  );
  const isDemo = connection.provider === 'demo_mailbox';
  return (
    <Card withBorder padding="md" radius="md">
      <Stack gap="sm">
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Stack gap={0} style={{ minWidth: 0 }}>
            <Text fw={600} style={{ overflowWrap: 'anywhere' }}>
              {connection.display_name ?? connection.email ?? 'Mailbox'}
            </Text>
            <Text size="xs" c="dimmed" style={{ overflowWrap: 'anywhere' }}>
              {connection.email ? <bdi>{connection.email}</bdi> : '—'} · {humanize(connection.provider)}
            </Text>
          </Stack>
          <Group gap="xs">
            {isDemo ? <MockBadge /> : null}
            <StatusBadge kind="connection" status={connection.status} size="sm" />
          </Group>
        </Group>
        {connection.last_error ? (
          <Alert color="red" variant="light" p="xs">
            <Text size="xs" style={{ overflowWrap: 'anywhere' }}>
              {connection.last_error}
            </Text>
          </Alert>
        ) : null}
        <FactList
          labelWidth={150}
          items={[
            { label: 'Connected', value: <TimeText value={connection.connected_at} size="sm" /> },
            {
              label: 'Granted scopes',
              value: connection.granted_scopes.length ? (
                <Group gap={4}>
                  {connection.granted_scopes.map((s) => (
                    <Code key={s} fz="xs">
                      {s}
                    </Code>
                  ))}
                </Group>
              ) : (
                '—'
              ),
            },
            {
              label: 'Sync',
              value: connection.sync ? (
                <Group gap={6} wrap="wrap">
                  <StatusBadge kind="sync" status={connection.sync.last_status} size="sm" />
                  <Text size="xs" c="dimmed">
                    <bdi>{connection.sync.folder}</bdi> ·{' '}
                    {formatInteger(connection.sync.messages_ingested_total)} ingested
                  </Text>
                </Group>
              ) : (
                'Never synced'
              ),
            },
            {
              label: 'Last sync completed',
              value: <TimeText value={connection.sync?.last_sync_completed_at} size="sm" />,
              hidden: !connection.sync,
            },
            {
              label: 'Last sync error',
              value: (
                <Text size="xs" c="red.7">
                  {connection.sync?.last_error}
                </Text>
              ),
              hidden: !connection.sync?.last_error,
            },
          ]}
        />
        {canSync ? (
          <Group gap="xs">
            <Button
              size="xs"
              variant="light"
              leftSection={<IconRefresh size={14} aria-hidden />}
              onClick={() => sync.mutate(connection.id)}
              loading={sync.isPending}
              disabled={connection.status !== 'active'}
            >
              Sync now
            </Button>
            {!isDemo ? (
              <Button
                size="xs"
                variant="subtle"
                color="red"
                leftSection={<IconPlugConnectedX size={14} aria-hidden />}
                onClick={confirm.open}
              >
                Disconnect
              </Button>
            ) : null}
          </Group>
        ) : null}
      </Stack>
      <ConfirmModal
        opened={confirmOpen}
        onClose={confirm.close}
        title="Disconnect this mailbox?"
        confirmLabel="Disconnect"
        confirmColor="red"
        loading={disconnect.isPending}
        onConfirm={() => disconnect.mutate()}
      >
        NUVEXA deletes the stored (encrypted) tokens for this mailbox and stops syncing. Already synced email
        and the audit history are kept.
      </ConfirmModal>
    </Card>
  );
}

function MicrosoftCard({ microsoft }: { microsoft: MicrosoftIntegrationOut }) {
  const canSync = useHasPermission('email:sync');
  return (
    <SectionCard
      title="Microsoft 365 mailbox"
      id="microsoft"
      icon={
        <ThemeIcon variant="light" color="blue" aria-hidden>
          <IconBrandOffice size={18} />
        </ThemeIcon>
      }
      actions={<ModeBadge mode={microsoft.mode} />}
      description="Reads mail through Microsoft Graph with delegated, read-only access. Tokens are encrypted server-side and never sent to the browser."
    >
      <Stack gap="md">
        <FactList
          labelWidth={150}
          items={[
            { label: 'Entra ID configured', value: <YesNo value={microsoft.entra_configured} /> },
            {
              label: 'Required scopes',
              value: (
                <Group gap={4}>
                  {microsoft.required_scopes.map((s) => (
                    <Code key={s} fz="xs">
                      {s}
                    </Code>
                  ))}
                </Group>
              ),
            },
          ]}
        />
        {canSync ? (
          microsoft.entra_configured ? (
            <Button
              component="a"
              href={navigationUrl('/integrations/microsoft/connect', '/integrations')}
              leftSection={<IconPlugConnected size={16} aria-hidden />}
              w="fit-content"
            >
              Connect a mailbox
            </Button>
          ) : (
            <Stack gap={4}>
              <Button
                disabled
                leftSection={<IconPlugConnected size={16} aria-hidden />}
                w="fit-content"
                aria-describedby="ms-connect-reason"
              >
                Connect a mailbox
              </Button>
              <Text size="xs" c="dimmed" id="ms-connect-reason">
                Microsoft Entra ID is not configured on the API. In demo mode the synthetic demo mailbox is
                used instead.
              </Text>
            </Stack>
          )
        ) : null}
        <Stack gap="sm">
          <Text size="sm" fw={600}>
            Connections ({microsoft.connections.length})
          </Text>
          {microsoft.connections.length === 0 ? (
            <Text size="sm" c="dimmed">
              No mailbox connected yet.
            </Text>
          ) : (
            microsoft.connections.map((connection) => (
              <ConnectionRow key={connection.id} connection={connection} />
            ))
          )}
        </Stack>
      </Stack>
    </SectionCard>
  );
}

/** The caller's own delegated Azure DevOps connection (entra_delegated mode). */
function AzureDevOpsConnection({ connection }: { connection: ConnectionOut }) {
  const [confirmOpen, confirm] = useDisclosure(false);
  const disconnect = useDisconnect(
    connection.id,
    confirm.close,
    'Your stored Azure DevOps tokens were deleted. Reconnect before executing approved work items.',
  );
  return (
    <Card withBorder padding="md" radius="md">
      <Stack gap="sm">
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Stack gap={0} style={{ minWidth: 0 }}>
            <Text fw={600} style={{ overflowWrap: 'anywhere' }}>
              {connection.display_name ?? connection.email ?? 'Azure DevOps account'}
            </Text>
            <Text size="xs" c="dimmed" style={{ overflowWrap: 'anywhere' }}>
              {connection.email ?? '—'}
            </Text>
          </Stack>
          <StatusBadge kind="connection" status={connection.status} size="sm" />
        </Group>
        {connection.last_error ? (
          <Alert color="red" variant="light" p="xs">
            <Text size="xs" style={{ overflowWrap: 'anywhere' }}>
              {connection.last_error}
            </Text>
          </Alert>
        ) : null}
        <FactList
          labelWidth={150}
          items={[{ label: 'Connected', value: <TimeText value={connection.connected_at} size="sm" /> }]}
        />
        <Button
          size="xs"
          variant="subtle"
          color="red"
          leftSection={<IconPlugConnectedX size={14} aria-hidden />}
          onClick={confirm.open}
          w="fit-content"
        >
          Disconnect Azure DevOps
        </Button>
      </Stack>
      <ConfirmModal
        opened={confirmOpen}
        onClose={confirm.close}
        title="Disconnect Azure DevOps?"
        confirmLabel="Disconnect"
        confirmColor="red"
        loading={disconnect.isPending}
        onConfirm={() => disconnect.mutate()}
      >
        NUVEXA deletes your stored (encrypted) Azure DevOps tokens. Approved work items cannot be created on
        your behalf until you reconnect. Existing work items and the audit history are kept.
      </ConfirmModal>
    </Card>
  );
}

function AzureDevOpsCard({ ado }: { ado: AzureDevOpsIntegrationOut }) {
  // Connecting delegates work-item creation to this user's account: the API requires action:execute.
  const canConnect = useHasPermission('action:execute');
  const [project, setProject] = useState<string | null>(ado.default_project);
  const [loadProjects, setLoadProjects] = useState(false);

  const test = useMutation({
    mutationFn: integrationsApi.testAzureDevOps,
    onError: (e) => notifyError('Connection test failed', e),
  });
  const projects = useQuery({
    queryKey: queryKeys.adoProjects,
    queryFn: integrationsApi.adoProjects,
    enabled: loadProjects,
    retry: false,
  });
  const types = useQuery({
    queryKey: queryKeys.adoWorkItemTypes(project ?? ''),
    queryFn: () => integrationsApi.adoWorkItemTypes(project!),
    enabled: loadProjects && Boolean(project) && projects.isSuccess,
    retry: false,
  });

  return (
    <SectionCard
      title="Azure DevOps"
      id="azure-devops"
      icon={
        <ThemeIcon variant="light" color="blue" aria-hidden>
          <IconBrandAzure size={18} />
        </ThemeIcon>
      }
      actions={<ModeBadge mode={ado.mode} />}
      description="Work items are created only after approval, by a deterministic adapter (REST api-version shown below) with tag-based idempotency."
    >
      <Stack gap="md">
        {/mock/i.test(ado.mode) ? (
          <Alert color="grape" variant="light" p="sm">
            <Text size="sm">
              Mock mode: work items are simulated and their URLs use <Code>mock://</Code>. Nothing is written
              to a real organization.
            </Text>
          </Alert>
        ) : null}
        <FactList
          labelWidth={170}
          items={[
            { label: 'Organization', value: ado.organization ?? '—' },
            { label: 'Configured', value: <YesNo value={ado.configured} /> },
            { label: 'Default project', value: ado.default_project ?? '—' },
            { label: 'Default work item type', value: ado.default_work_item_type },
            { label: 'API version', value: <Code fz="xs">{ado.api_version}</Code> },
            {
              label: 'Per-user connection',
              value: ado.requires_user_connection ? (
                <YesNo value={ado.user_connected} yes="Connected" no="Not connected" />
              ) : (
                'Not required'
              ),
            },
          ]}
        />
        {ado.connection ? <AzureDevOpsConnection connection={ado.connection} /> : null}
        <Group gap="sm">
          <Button
            variant="light"
            leftSection={<IconTestPipe size={16} aria-hidden />}
            onClick={() => test.mutate()}
            loading={test.isPending}
          >
            Test connection
          </Button>
          <Button
            variant="default"
            onClick={() => setLoadProjects(true)}
            loading={projects.isFetching}
            disabled={loadProjects && projects.isSuccess}
          >
            List projects
          </Button>
          {ado.requires_user_connection && canConnect ? (
            <Button
              component="a"
              href={navigationUrl('/integrations/azure-devops/connect', '/integrations')}
              leftSection={<IconPlugConnected size={16} aria-hidden />}
              variant={ado.user_connected ? 'default' : 'filled'}
            >
              {ado.user_connected ? 'Reconnect Azure DevOps' : 'Connect Azure DevOps'}
            </Button>
          ) : null}
        </Group>
        {test.data ? (
          <Alert
            color={test.data.ok ? 'green' : 'red'}
            variant="light"
            title={test.data.ok ? 'Connection OK' : 'Connection failed'}
            role="status"
          >
            <Text size="sm">{test.data.message}</Text>
            <Text size="xs" c="dimmed">
              Mode {test.data.mode} · organization <bdi>{test.data.organization ?? '—'}</bdi> ·{' '}
              {formatLatency(test.data.latency_ms)}
            </Text>
          </Alert>
        ) : null}
        {loadProjects ? (
          projects.isPending ? (
            <Skeleton height={60} />
          ) : projects.isError ? (
            <ErrorState error={projects.error} compact onRetry={() => void projects.refetch()} />
          ) : (
            <Stack gap="sm">
              <Select
                label="Project"
                placeholder={projects.data.items.length ? 'Choose a project' : 'No projects visible'}
                data={projects.data.items.map((p) => ({
                  value: p.name,
                  label: p.state ? `${p.name} (${p.state})` : p.name,
                }))}
                value={project}
                onChange={setProject}
                searchable
                w={320}
                maw="100%"
              />
              {project ? (
                types.isPending ? (
                  <Skeleton height={80} />
                ) : types.isError ? (
                  <ErrorState error={types.error} compact onRetry={() => void types.refetch()} />
                ) : (
                  <Table.ScrollContainer minWidth={420}>
                    <Table aria-label={`Work item types in ${project}`} verticalSpacing="xs">
                      <Table.Thead>
                        <Table.Tr>
                          <Table.Th>Work item type</Table.Th>
                          <Table.Th>Reference name</Table.Th>
                          <Table.Th>State</Table.Th>
                        </Table.Tr>
                      </Table.Thead>
                      <Table.Tbody>
                        {types.data.items.map((t) => (
                          <Table.Tr key={t.name}>
                            <Table.Td>
                              <Tooltip label={t.description ?? ''} disabled={!t.description}>
                                <Text size="sm">{t.name}</Text>
                              </Tooltip>
                            </Table.Td>
                            <Table.Td>
                              <Code fz="xs">{t.reference_name ?? '—'}</Code>
                            </Table.Td>
                            <Table.Td>
                              {t.is_disabled ? (
                                <Badge color="gray" size="sm">
                                  Disabled
                                </Badge>
                              ) : (
                                <Badge color="green" variant="light" size="sm">
                                  Enabled
                                </Badge>
                              )}
                            </Table.Td>
                          </Table.Tr>
                        ))}
                      </Table.Tbody>
                    </Table>
                  </Table.ScrollContainer>
                )
              ) : null}
              {projects.data.items.find((p) => p.name === project)?.url ? (
                <ExternalLink url={projects.data.items.find((p) => p.name === project)?.url}>
                  Open project
                </ExternalLink>
              ) : null}
            </Stack>
          )
        ) : null}
      </Stack>
    </SectionCard>
  );
}

function Diagnostics({ demoMode }: { demoMode: boolean }) {
  const health = useQuery({ queryKey: queryKeys.health, queryFn: healthApi.ready, retry: false });
  const config = useAuthConfig();
  const { user, permissions } = useSession();
  return (
    <SectionCard
      title="Diagnostics"
      id="diagnostics"
      icon={
        <ThemeIcon variant="light" color="gray" aria-hidden>
          <IconStethoscope size={18} />
        </ThemeIcon>
      }
      actions={
        <Button
          size="xs"
          variant="subtle"
          leftSection={<IconRefresh size={14} aria-hidden />}
          onClick={() => void health.refetch()}
          loading={health.isFetching}
        >
          Re-check
        </Button>
      }
    >
      <Grid gap="md">
        <Grid.Col span={{ base: 12, md: 6 }}>
          {health.isPending ? (
            <Skeleton height={80} />
          ) : health.isError ? (
            <ErrorState error={health.error} compact title="API readiness check failed" />
          ) : (
            <Stack gap="xs">
              <Group gap="xs">
                <Text size="sm" fw={600}>
                  API readiness
                </Text>
                <Badge
                  color={
                    health.data.status === 'ok'
                      ? 'green'
                      : health.data.status === 'degraded'
                        ? 'yellow'
                        : 'red'
                  }
                  variant="light"
                >
                  {humanize(health.data.status)}
                </Badge>
                <Text size="xs" c="dimmed">
                  v{health.data.version}
                </Text>
              </Group>
              <List size="sm" spacing={2}>
                {Object.entries(health.data.checks).map(([name, value]) => (
                  <List.Item key={name}>
                    {humanize(name)}: <b>{value}</b>
                  </List.Item>
                ))}
              </List>
            </Stack>
          )}
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 6 }}>
          <FactList
            labelWidth={140}
            items={[
              { label: 'Environment', value: config.data?.environment ?? '—' },
              { label: 'Demo mode', value: <YesNo value={demoMode} /> },
              { label: 'Tenant', value: user.tenant_name },
              { label: 'Your role', value: humanize(user.role) },
              {
                label: 'Permissions',
                value: (
                  <Group gap={4}>
                    {permissions.map((p) => (
                      <Code key={p} fz="xs">
                        {p}
                      </Code>
                    ))}
                  </Group>
                ),
              },
              { label: 'Dashboard build', value: <Code fz="xs">{import.meta.env.MODE}</Code> },
            ]}
          />
        </Grid.Col>
      </Grid>
    </SectionCard>
  );
}

export function IntegrationsPage() {
  const integrations = useQuery({ queryKey: queryKeys.integrations, queryFn: integrationsApi.get });

  return (
    <>
      <PageHeader
        title="Integrations"
        description="Connections NUVEXA uses to read email, write approved work and analyze content. Credentials never reach the browser."
      />
      {integrations.isPending ? (
        <Stack gap="md" role="status" aria-busy="true" aria-label="Loading integrations">
          <Skeleton height={220} />
          <Skeleton height={220} />
        </Stack>
      ) : integrations.isError ? (
        <ErrorState
          error={integrations.error}
          onRetry={() => void integrations.refetch()}
          retrying={integrations.isFetching}
        />
      ) : (
        <Stack gap="md">
          {integrations.data.demo_mode ? (
            <Alert color="grape" variant="light" title="Demo mode">
              A synthetic mailbox, the heuristic analysis agent and a mock Azure DevOps are active. No
              external system is contacted.
            </Alert>
          ) : null}
          <Grid gap="md" align="flex-start">
            <Grid.Col span={{ base: 12, xl: 6 }}>
              <MicrosoftCard microsoft={integrations.data.microsoft} />
            </Grid.Col>
            <Grid.Col span={{ base: 12, xl: 6 }}>
              <Stack gap="md">
                <AzureDevOpsCard ado={integrations.data.azure_devops} />
                <SectionCard
                  title="LLM provider"
                  id="llm"
                  icon={
                    <ThemeIcon variant="light" color="nuvexa" aria-hidden>
                      <IconBrain size={18} />
                    </ThemeIcon>
                  }
                  actions={<ModeBadge mode={integrations.data.llm.provider} />}
                  description="The model only proposes. Validation, policy, approval and execution are deterministic code."
                >
                  <FactList
                    labelWidth={170}
                    items={[
                      { label: 'Provider', value: integrations.data.llm.provider },
                      { label: 'Model', value: <Code fz="xs">{integrations.data.llm.model}</Code> },
                      {
                        label: 'Structured outputs',
                        value: <YesNo value={integrations.data.llm.structured_outputs} />,
                      },
                      {
                        label: 'Observability',
                        value: (
                          <YesNo
                            value={integrations.data.llm.observability_enabled}
                            yes="Enabled"
                            no="Disabled"
                          />
                        ),
                      },
                    ]}
                  />
                </SectionCard>
              </Stack>
            </Grid.Col>
          </Grid>
          <Diagnostics demoMode={integrations.data.demo_mode} />
        </Stack>
      )}
    </>
  );
}
