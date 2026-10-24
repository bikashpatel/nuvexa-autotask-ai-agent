import {
  Affix,
  Alert,
  Badge,
  Button,
  Fieldset,
  Grid,
  Group,
  List,
  NumberInput,
  Paper,
  Select,
  Skeleton,
  Stack,
  Switch,
  Table,
  TagsInput,
  Text,
  TextInput,
  Textarea,
} from '@mantine/core';
import { useForm } from '@mantine/form';
import { IconDeviceFloppy, IconLock, IconRefresh } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { isApiError, type ApiError } from '../../api/client';
import { settingsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import { EMAIL_CATEGORIES, PRIORITIES, type SettingsOut } from '../../api/types';
import { ErrorState } from '../../components/ErrorState';
import { PageHeader } from '../../components/PageHeader';
import { SectionCard } from '../../components/SectionCard';
import { TimeText } from '../../components/TimeText';
import { ADO_PRIORITY_LABELS, CATEGORY_LABELS, PRIORITY_LABELS } from '../../lib/format';
import { notifyError, notifySuccess } from '../../lib/notify';
import { useHasPermission } from '../auth/session';
import { formToSettings, settingsToForm, validateSettings, type SettingsFormValues } from './settingsForm';

const ADO_PRIORITY_OPTIONS = [1, 2, 3, 4].map((n) => ({
  value: String(n),
  label: ADO_PRIORITY_LABELS[n] ?? String(n),
}));
const PRIORITY_OPTIONS = PRIORITIES.map((p) => ({ value: p, label: PRIORITY_LABELS[p] }));

function SettingsForm({
  data,
  canWrite,
  onConflict,
}: {
  data: SettingsOut;
  canWrite: boolean;
  onConflict: (e: ApiError) => void;
}) {
  const queryClient = useQueryClient();
  const form = useForm<SettingsFormValues>({
    mode: 'controlled',
    initialValues: settingsToForm(data.settings),
    validate: validateSettings,
    validateInputOnBlur: true,
  });
  const [serverErrors, setServerErrors] = useState<string[]>([]);

  const save = useMutation({
    mutationFn: (values: SettingsFormValues) =>
      settingsApi.update({ expected_version: data.version, settings: formToSettings(values) }),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKeys.settings, next);
      notifySuccess('Settings saved', `Tenant settings are now at version ${next.version}.`);
    },
    onError: (error) => {
      if (isApiError(error) && error.isStaleVersion) {
        onConflict(error);
      } else if (isApiError(error) && error.isValidation) {
        // Field-level errors are attached to inputs; all are also listed (model-level validators,
        // e.g. "medium threshold must be <= high threshold", have no single input).
        const fieldErrors: Record<string, string> = {};
        for (const fe of error.fieldErrors) if (fe.field) fieldErrors[fe.field] = fe.message;
        form.setErrors(fieldErrors);
        const messages = error.fieldErrors.map((fe) =>
          fe.field ? `${fe.field}: ${fe.message}` : fe.message,
        );
        setServerErrors(messages.length ? messages : [error.message]);
      } else {
        notifyError('Could not save settings', error);
      }
    },
  });

  const submit = form.onSubmit((values) => {
    setServerErrors([]);
    save.mutate(values);
  });
  const allowedTypes = form.values.azure_devops.allowed_work_item_types;
  const dirty = form.isDirty();

  return (
    <form onSubmit={submit} noValidate>
      <Fieldset disabled={!canWrite} variant="unstyled">
        <Stack gap="md">
          {serverErrors.length ? (
            <Alert color="red" variant="light" title="The server rejected these settings" role="alert">
              <List size="sm">
                {serverErrors.map((e) => (
                  <List.Item key={e}>{e}</List.Item>
                ))}
              </List>
            </Alert>
          ) : null}

          <SectionCard
            title="Azure DevOps defaults"
            id="ado-defaults"
            description="Used by the planner and enforced by policy validation. The model can never choose projects, types or fields outside these lists."
          >
            <Stack gap="sm">
              <Grid gap="sm">
                <Grid.Col span={{ base: 12, sm: 6 }}>
                  <TextInput
                    label="Default project"
                    placeholder="None"
                    {...form.getInputProps('azure_devops.project')}
                  />
                </Grid.Col>
                <Grid.Col span={{ base: 12, sm: 6 }}>
                  <TextInput
                    label="Default work item type"
                    {...form.getInputProps('azure_devops.work_item_type')}
                  />
                </Grid.Col>
                <Grid.Col span={{ base: 12, sm: 6 }}>
                  <TextInput
                    label="Default area path"
                    placeholder="None"
                    {...form.getInputProps('azure_devops.area_path')}
                  />
                </Grid.Col>
                <Grid.Col span={{ base: 12, sm: 6 }}>
                  <TextInput
                    label="Default iteration path"
                    placeholder="None"
                    {...form.getInputProps('azure_devops.iteration_path')}
                  />
                </Grid.Col>
              </Grid>
              <TagsInput
                label="Allowed projects"
                description="Empty → any project returned by the connection is allowed."
                placeholder="Add a project and press Enter"
                clearable
                {...form.getInputProps('azure_devops.allowed_projects')}
              />
              <TagsInput
                label="Allowed work item types"
                placeholder="Add a type and press Enter"
                clearable
                {...form.getInputProps('azure_devops.allowed_work_item_types')}
              />
              <TagsInput
                label="Allowed additional fields"
                description="Field reference names reviewers and the planner may set besides title, description, priority and tags."
                placeholder="e.g. Microsoft.VSTS.Common.Severity"
                clearable
                {...form.getInputProps('azure_devops.allowed_additional_fields')}
              />
              <Text size="sm" fw={600} mt="xs">
                Category → work item type
              </Text>
              <Table.ScrollContainer minWidth={420}>
                <Table aria-label="Work item type per category" verticalSpacing={6} highlightOnHover={false}>
                  <Table.Tbody>
                    {EMAIL_CATEGORIES.map((c) => (
                      <Table.Tr key={c}>
                        <Table.Td w="40%">
                          <Text size="sm">{CATEGORY_LABELS[c]}</Text>
                        </Table.Td>
                        <Table.Td>
                          <Select
                            aria-label={`Work item type for ${CATEGORY_LABELS[c]}`}
                            placeholder="No work item (default type when proposed)"
                            data={Array.from(
                              new Set(
                                [
                                  ...allowedTypes,
                                  form.values.azure_devops.category_work_item_types[c],
                                ].filter(Boolean),
                              ),
                            )}
                            clearable
                            {...form.getInputProps(`azure_devops.category_work_item_types.${c}`)}
                            onChange={(value: string | null) =>
                              form.setFieldValue(`azure_devops.category_work_item_types.${c}`, value ?? '')
                            }
                            value={form.values.azure_devops.category_work_item_types[c] || null}
                          />
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            </Stack>
          </SectionCard>

          <SectionCard
            title="Classification categories"
            id="taxonomy"
            description="Disabled categories are not offered to the classifier. “Other” is the fallback and cannot be disabled. Descriptions are sent to the model as guidance."
          >
            <Stack gap="sm">
              {EMAIL_CATEGORIES.map((c) => (
                <Paper key={c} withBorder p="sm" radius="md">
                  <Group justify="space-between" mb={6} wrap="wrap">
                    <Text fw={600} size="sm">
                      {CATEGORY_LABELS[c]}
                    </Text>
                    <Switch
                      label={c === 'other' ? 'Always enabled' : 'Enabled'}
                      disabled={c === 'other' || !canWrite}
                      {...form.getInputProps(`taxonomy.enabled.${c}`, { type: 'checkbox' })}
                    />
                  </Group>
                  <Textarea
                    aria-label={`Description for ${CATEGORY_LABELS[c]}`}
                    autosize
                    minRows={1}
                    maxRows={4}
                    {...form.getInputProps(`taxonomy.category_descriptions.${c}`)}
                  />
                </Paper>
              ))}
            </Stack>
          </SectionCard>

          <Grid gap="md">
            <Grid.Col span={{ base: 12, lg: 7 }}>
              <SectionCard
                title="Priority mapping"
                id="priority"
                description="Default business priority per category, and how each priority maps to Microsoft.VSTS.Common.Priority (1 = highest)."
              >
                <Grid gap="md">
                  <Grid.Col span={{ base: 12, sm: 7 }}>
                    <Stack gap={6}>
                      {EMAIL_CATEGORIES.map((c) => (
                        <Group key={c} justify="space-between" wrap="nowrap" gap="xs">
                          <Text size="sm">{CATEGORY_LABELS[c]}</Text>
                          <Select
                            aria-label={`Default priority for ${CATEGORY_LABELS[c]}`}
                            data={PRIORITY_OPTIONS}
                            allowDeselect={false}
                            w={140}
                            {...form.getInputProps(`priority.category_default_priority.${c}`)}
                          />
                        </Group>
                      ))}
                    </Stack>
                  </Grid.Col>
                  <Grid.Col span={{ base: 12, sm: 5 }}>
                    <Stack gap={6}>
                      <Text size="sm" fw={600}>
                        Azure DevOps priority
                      </Text>
                      {PRIORITIES.map((p) => (
                        <Select
                          key={p}
                          label={PRIORITY_LABELS[p]}
                          data={ADO_PRIORITY_OPTIONS}
                          allowDeselect={false}
                          {...form.getInputProps(`priority.azure_devops_priority.${p}`)}
                        />
                      ))}
                    </Stack>
                  </Grid.Col>
                </Grid>
              </SectionCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, lg: 5 }}>
              <Stack gap="md">
                <SectionCard
                  title="Confidence thresholds"
                  id="confidence"
                  description="Buckets for the heuristic confidence score (not a calibrated probability)."
                >
                  <Group grow>
                    <NumberInput
                      label="High ≥"
                      min={0}
                      max={1}
                      step={0.05}
                      decimalScale={2}
                      {...form.getInputProps('confidence.high')}
                    />
                    <NumberInput
                      label="Medium ≥"
                      min={0}
                      max={1}
                      step={0.05}
                      decimalScale={2}
                      {...form.getInputProps('confidence.medium')}
                    />
                  </Group>
                </SectionCard>
                <SectionCard title="Retention" id="retention" description="Days before data is purged.">
                  <Stack gap="sm">
                    <NumberInput
                      label="Email bodies (days)"
                      min={1}
                      max={3650}
                      allowDecimal={false}
                      {...form.getInputProps('retention.email_body_days')}
                    />
                    <NumberInput
                      label="Workflow events (days)"
                      min={1}
                      max={3650}
                      allowDecimal={false}
                      {...form.getInputProps('retention.workflow_event_days')}
                    />
                    <NumberInput
                      label="Audit events (days)"
                      min={30}
                      max={3650}
                      allowDecimal={false}
                      {...form.getInputProps('retention.audit_days')}
                    />
                  </Stack>
                </SectionCard>
                <SectionCard title="Automation" id="automation">
                  <Stack gap="sm">
                    <Switch
                      label="Auto-analyze new emails"
                      description="Start an analysis automatically after each sync. Proposals still require approval."
                      {...form.getInputProps('auto_analyze_new_emails', { type: 'checkbox' })}
                    />
                    <Switch
                      label="Attachment processing"
                      description="Not available in this version — attachment content is never downloaded."
                      checked={false}
                      disabled
                      readOnly
                    />
                  </Stack>
                </SectionCard>
              </Stack>
            </Grid.Col>
          </Grid>
        </Stack>
      </Fieldset>

      {canWrite && dirty ? (
        <Affix position={{ bottom: 20, right: 20 }}>
          <Paper shadow="lg" p="sm" withBorder radius="md">
            <Group gap="sm">
              <Text size="sm">Unsaved changes</Text>
              <Button variant="default" onClick={() => form.reset()} disabled={save.isPending}>
                Discard
              </Button>
              <Button
                type="submit"
                leftSection={<IconDeviceFloppy size={16} aria-hidden />}
                loading={save.isPending}
              >
                Save settings
              </Button>
            </Group>
          </Paper>
        </Affix>
      ) : null}
    </form>
  );
}

export function SettingsPage() {
  const canWrite = useHasPermission('settings:write');
  const settings = useQuery({ queryKey: queryKeys.settings, queryFn: settingsApi.get });
  const [conflict, setConflict] = useState<ApiError | null>(null);

  return (
    <>
      <PageHeader
        title="Settings"
        description="Tenant-wide configuration for analysis, proposals and data retention."
        actions={
          settings.data ? (
            <Group gap="xs">
              <Badge variant="outline" color="gray">
                Version {settings.data.version}
              </Badge>
              <Text size="xs" c="dimmed">
                Updated <TimeText value={settings.data.updated_at} size="xs" />
              </Text>
            </Group>
          ) : null
        }
      />
      {!canWrite ? (
        <Alert color="gray" variant="light" icon={<IconLock size={18} aria-hidden />} mb="md">
          Read-only: only tenant admins can change settings.
        </Alert>
      ) : null}
      {conflict ? (
        <Alert
          color="orange"
          variant="filled"
          mb="md"
          role="alert"
          title="Settings changed — reload before saving"
          icon={<IconRefresh size={18} aria-hidden />}
        >
          <Stack gap="xs">
            <Text size="sm">
              Another admin saved the settings after you loaded them. Reload to see the latest version, then
              re-apply your changes.
            </Text>
            <Button
              size="xs"
              color="dark"
              w="fit-content"
              onClick={() => void settings.refetch().then(() => setConflict(null))}
              loading={settings.isFetching}
            >
              Reload settings
            </Button>
          </Stack>
        </Alert>
      ) : null}
      {settings.isPending ? (
        <Stack gap="md" role="status" aria-busy="true" aria-label="Loading settings">
          <Skeleton height={260} />
          <Skeleton height={260} />
        </Stack>
      ) : settings.isError ? (
        <ErrorState
          error={settings.error}
          onRetry={() => void settings.refetch()}
          retrying={settings.isFetching}
        />
      ) : (
        <SettingsForm
          key={settings.data.version}
          data={settings.data}
          canWrite={canWrite && !conflict}
          onConflict={setConflict}
        />
      )}
    </>
  );
}
