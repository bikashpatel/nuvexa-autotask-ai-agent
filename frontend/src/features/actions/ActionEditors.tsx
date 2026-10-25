import {
  ActionIcon,
  Alert,
  Autocomplete,
  Button,
  Fieldset,
  Group,
  List,
  Select,
  SimpleGrid,
  Stack,
  TagsInput,
  Text,
  TextInput,
  Textarea,
  Tooltip,
} from '@mantine/core';
import { DateTimePicker } from '@mantine/dates';
import { useForm, type UseFormReturnType } from '@mantine/form';
import { IconChecks, IconPlus, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useId, useRef, useState, type ReactNode } from 'react';

import { isApiError, type ApiError } from '../../api/client';
import { integrationsApi } from '../../api/endpoints';
import { queryKeys } from '../../api/queryKeys';
import type {
  ActionDetail,
  ActionPayload,
  AdoValidateOut,
  AzureDevOpsWorkItemPayload,
  EmailReplyDraftPayload,
  FollowUpTaskPayload,
  NoActionPayload,
} from '../../api/types';
import { ADO_PRIORITY_LABELS } from '../../lib/format';
import { notifyError, notifySuccess } from '../../lib/notify';
import { ActionControls } from './ActionControls';
import { useUpdateAction } from './hooks';
import {
  DATE_TIME_FORMAT,
  LIMITS,
  adoToForm,
  adoToPayload,
  compactErrors,
  followUpToForm,
  followUpToPayload,
  mapServerFieldErrors,
  noActionToForm,
  noActionToPayload,
  replyToForm,
  replyToPayload,
  validateAdo,
  validateFollowUp,
  validateNoAction,
  validateReply,
  validateTag,
  type AdoFormValues,
  type Errors,
  type FollowUpFormValues,
  type NoActionFormValues,
  type ReplyFormValues,
} from './payloadForms';

export interface EditorProps {
  action: ActionDetail;
  editable: boolean;
  onSaved: (previous: ActionDetail, next: ActionDetail) => void;
  onConflict: (error: ApiError) => void;
  /** An execute/reconcile job is queued and not yet picked up by a worker. */
  queued: boolean;
  /** The watch on a queued job expired with no visible change (polling stopped). */
  queueStale?: boolean;
  onQueued: (updatedAt: string) => void;
}

function counter(value: string, max: number): string {
  return `${value.length.toLocaleString()}/${max.toLocaleString()}`;
}

/**
 * Shared editor behaviour: client validation, PATCH with expected_version, 422 → field errors,
 * 409 stale_version → conflict banner. The form is remounted (keyed by version) after a save.
 */
function useActionEditor<V extends object>(
  props: EditorProps,
  initialValues: V,
  validate: (values: V) => Errors,
  toPayload: (values: V) => ActionPayload,
) {
  const { action, onSaved, onConflict } = props;
  const form = useForm<V>({
    mode: 'controlled',
    initialValues,
    validate: (values) => compactErrors(validate(values)),
    validateInputOnBlur: true,
  });
  const update = useUpdateAction(action.id);
  const [generalErrors, setGeneralErrors] = useState<string[]>([]);

  // Synchronous single-flight: a second click before the re-render must not PATCH the same
  // expected_version twice (the duplicate would come back 409 and show a false conflict).
  const saveInFlight = useRef(false);

  const save = () => {
    if (saveInFlight.current) return;
    if (form.validate().hasErrors) return;
    saveInFlight.current = true;
    setGeneralErrors([]);
    update.mutate(
      { expectedVersion: action.version, payload: toPayload(form.getValues()) },
      {
        onSettled: () => {
          saveInFlight.current = false;
        },
        onSuccess: (next) => onSaved(action, next),
        onError: (error) => {
          if (isApiError(error) && error.isStaleVersion) {
            onConflict(error);
          } else if (isApiError(error) && error.isValidation) {
            const { fieldErrors, general } = mapServerFieldErrors(
              error.fieldErrors,
              Object.keys(initialValues),
            );
            form.setErrors(fieldErrors);
            setGeneralErrors(
              general.length ? general : Object.keys(fieldErrors).length ? [] : [error.message],
            );
          } else {
            notifyError('Could not save changes', error);
          }
        },
      },
    );
  };

  const controls = (
    <ActionControls
      action={action}
      dirty={form.isDirty()}
      editable={props.editable}
      saving={update.isPending}
      onSave={save}
      onDiscard={() => {
        form.reset();
        setGeneralErrors([]);
      }}
      onConflict={onConflict}
      queued={props.queued}
      queueStale={props.queueStale}
      onQueued={props.onQueued}
    />
  );

  return { form, controls, generalErrors, saving: update.isPending };
}

function EditorShell({
  editable,
  generalErrors,
  children,
  controls,
  extra,
}: {
  editable: boolean;
  generalErrors: string[];
  children: ReactNode;
  controls: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <Stack gap="md">
      {generalErrors.length > 0 ? (
        <Alert color="red" variant="light" title="The server rejected these changes" role="alert">
          <List size="sm">
            {generalErrors.map((message) => (
              <List.Item key={message}>{message}</List.Item>
            ))}
          </List>
        </Alert>
      ) : null}
      <Fieldset
        disabled={!editable}
        variant="unstyled"
        legend={
          <Text size="xs" c="dimmed">
            {editable
              ? 'Edit the payload before approving. Saving creates a new version.'
              : 'Payload (read-only)'}
          </Text>
        }
      >
        <Stack gap="sm">{children}</Stack>
      </Fieldset>
      {extra}
      {controls}
    </Stack>
  );
}

// --- Azure DevOps --------------------------------------------------------------------------------

function AdoValidationResult({ result }: { result: AdoValidateOut }) {
  const issues = [
    ...result.schema_issues.map((i) => `${i.field ? `${i.field}: ` : ''}${i.message}`),
    ...result.policy_issues,
    ...result.server_issues.map((i) => `${i.field ? `${i.field}: ` : ''}${i.message}`),
  ];
  return (
    <Alert
      color={result.valid ? 'green' : 'red'}
      variant="light"
      title={result.valid ? 'Payload is valid' : 'Payload has issues'}
      role="status"
    >
      <Stack gap={4}>
        <Text size="xs" c="dimmed">
          {result.server_validated
            ? 'Validated by Azure DevOps (validateOnly — nothing was created).'
            : 'Validated against the schema and tenant policy (no server-side check).'}
        </Text>
        {issues.length ? (
          <List size="sm">
            {issues.map((issue) => (
              <List.Item key={issue}>{issue}</List.Item>
            ))}
          </List>
        ) : null}
      </Stack>
    </Alert>
  );
}

function AdditionalFields({ form }: { form: UseFormReturnType<AdoFormValues> }) {
  const rows = form.values.additional_fields;
  return (
    <Stack gap={6}>
      <Group justify="space-between">
        <Text size="sm" fw={500}>
          Additional fields
        </Text>
        <Button
          size="compact-xs"
          variant="subtle"
          leftSection={<IconPlus size={12} aria-hidden />}
          disabled={rows.length >= LIMITS.ado.additionalFields}
          onClick={() => form.insertListItem('additional_fields', { name: '', value: '', numeric: false })}
        >
          Add field
        </Button>
      </Group>
      {rows.length === 0 ? (
        <Text size="xs" c="dimmed">
          None. Only fields allowed by tenant settings are accepted.
        </Text>
      ) : null}
      {typeof form.errors.additional_fields === 'string' ? (
        <Text size="xs" c="red">
          {form.errors.additional_fields}
        </Text>
      ) : null}
      {rows.map((_, index) => (
        <Group key={index} gap="xs" align="flex-start" wrap="nowrap">
          <TextInput
            aria-label={`Additional field ${index + 1} reference name`}
            placeholder="Microsoft.VSTS.Common.Severity"
            style={{ flex: 1 }}
            {...form.getInputProps(`additional_fields.${index}.name`)}
          />
          <TextInput
            aria-label={`Additional field ${index + 1} value`}
            placeholder="Value"
            style={{ flex: 1 }}
            {...form.getInputProps(`additional_fields.${index}.value`)}
          />
          <ActionIcon
            color="red"
            variant="subtle"
            mt={4}
            onClick={() => form.removeListItem('additional_fields', index)}
            aria-label={`Remove additional field ${index + 1}`}
          >
            <IconTrash size={16} aria-hidden />
          </ActionIcon>
        </Group>
      ))}
    </Stack>
  );
}

export function AdoEditor(props: EditorProps) {
  const original = props.action.payload as Partial<AzureDevOpsWorkItemPayload>;
  const { form, controls, generalErrors } = useActionEditor<AdoFormValues>(
    props,
    adoToForm(original),
    validateAdo,
    (values) => adoToPayload(values, original),
  );
  const [validation, setValidation] = useState<AdoValidateOut | null>(null);

  const projects = useQuery({
    queryKey: queryKeys.adoProjects,
    queryFn: integrationsApi.adoProjects,
    staleTime: 5 * 60_000,
    retry: false,
    enabled: props.editable,
  });
  const project = form.values.project.trim();
  const types = useQuery({
    queryKey: queryKeys.adoWorkItemTypes(project),
    queryFn: () => integrationsApi.adoWorkItemTypes(project),
    staleTime: 5 * 60_000,
    retry: false,
    enabled:
      props.editable && Boolean(project) && Boolean(projects.data?.items.some((p) => p.name === project)),
  });

  const validate = useMutation({
    mutationFn: () =>
      integrationsApi.validateAdoPayload({
        payload: adoToPayload(form.getValues(), original) as unknown as Record<string, unknown>,
        server_side: true,
      }),
    onSuccess: (result) => {
      setValidation(result);
      if (result.valid) notifySuccess('Payload is valid');
    },
    onError: (error) => notifyError('Validation request failed', error),
  });

  return (
    <EditorShell
      editable={props.editable}
      generalErrors={generalErrors}
      controls={controls}
      extra={
        <>
          {props.editable ? (
            <Group>
              <Tooltip label="Check the payload against the schema, tenant policy and Azure DevOps (validateOnly). Nothing is created.">
                <Button
                  size="xs"
                  variant="light"
                  color="gray"
                  leftSection={<IconChecks size={14} aria-hidden />}
                  onClick={() => validate.mutate()}
                  loading={validate.isPending}
                >
                  Validate with Azure DevOps
                </Button>
              </Tooltip>
            </Group>
          ) : null}
          {validation ? <AdoValidationResult result={validation} /> : null}
        </>
      }
    >
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
        <Autocomplete
          label="Project"
          withAsterisk
          data={projects.data?.items.map((p) => p.name) ?? []}
          maxLength={LIMITS.ado.project}
          {...form.getInputProps('project')}
        />
        <Autocomplete
          label="Work item type"
          withAsterisk
          data={types.data?.items.filter((t) => !t.is_disabled).map((t) => t.name) ?? []}
          maxLength={LIMITS.ado.workItemType}
          {...form.getInputProps('work_item_type')}
        />
      </SimpleGrid>
      <TextInput
        label="Title"
        withAsterisk
        description={counter(form.values.title, LIMITS.ado.title)}
        {...form.getInputProps('title')}
      />
      <Textarea
        label="Description"
        autosize
        minRows={4}
        maxRows={14}
        description={counter(form.values.description, LIMITS.ado.description)}
        {...form.getInputProps('description')}
      />
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
        <Select
          label="Priority"
          withAsterisk
          allowDeselect={false}
          data={[1, 2, 3, 4].map((p) => ({ value: String(p), label: ADO_PRIORITY_LABELS[p] ?? String(p) }))}
          description="Microsoft.VSTS.Common.Priority (1 = highest)"
          {...form.getInputProps('priority')}
        />
        <TextInput
          label="Assigned to"
          placeholder="name@company.com"
          type="email"
          {...form.getInputProps('assigned_to')}
        />
        <TextInput label="Area path" placeholder="Project\\Team" {...form.getInputProps('area_path')} />
        <TextInput
          label="Iteration path"
          placeholder="Project\\Sprint 1"
          {...form.getInputProps('iteration_path')}
        />
      </SimpleGrid>
      <TagsInput
        label="Tags"
        description={`Up to ${LIMITS.ado.tags}. Letters, digits, spaces and _ . - : / (no “;”). NUVEXA adds its idempotency tag automatically.`}
        maxTags={LIMITS.ado.tags}
        splitChars={[',']}
        clearable
        onDuplicate={() => undefined}
        {...form.getInputProps('tags')}
        error={form.errors.tags ?? form.values.tags.map(validateTag).find(Boolean) ?? undefined}
      />
      <AdditionalFields form={form} />
    </EditorShell>
  );
}

// --- Reply draft ---------------------------------------------------------------------------------

export function ReplyEditor(props: EditorProps) {
  const original = props.action.payload as Partial<EmailReplyDraftPayload>;
  const { form, controls, generalErrors } = useActionEditor<ReplyFormValues>(
    props,
    replyToForm(original),
    validateReply,
    (values) => replyToPayload(values, original),
  );
  return (
    <EditorShell editable={props.editable} generalErrors={generalErrors} controls={controls}>
      <Alert color="blue" variant="light" p="xs">
        <Text size="xs">
          Drafts are never sent by NUVEXA. Approving stores the draft for you to copy and send yourself.
        </Text>
      </Alert>
      <TextInput
        label="To"
        value={(original.to ?? []).join(', ')}
        readOnly
        disabled
        description="Always the original sender (enforced by the server)."
      />
      <TextInput
        label="Subject"
        withAsterisk
        description={counter(form.values.subject, LIMITS.reply.subject)}
        {...form.getInputProps('subject')}
      />
      <Textarea
        label="Body"
        withAsterisk
        autosize
        minRows={6}
        maxRows={18}
        description={counter(form.values.body, LIMITS.reply.body)}
        {...form.getInputProps('body')}
      />
    </EditorShell>
  );
}

// --- Follow-up task ------------------------------------------------------------------------------

// Mantine's picker controls (clear ×, month navigation, submit ✓) are icon-only without names.
const DUE_DATE_CLEAR_PROPS = { 'aria-label': 'Clear due date' };
const DUE_DATE_SUBMIT_PROPS = { 'aria-label': 'Apply due date' };
const DUE_DATE_ARIA_LABELS = {
  previousMonth: 'Previous month',
  nextMonth: 'Next month',
  previousYear: 'Previous year',
  nextYear: 'Next year',
  previousDecade: 'Previous decade',
  nextDecade: 'Next decade',
  monthLevelControl: 'Choose month',
  yearLevelControl: 'Choose year',
};

export function FollowUpEditor(props: EditorProps) {
  const original = props.action.payload as Partial<FollowUpTaskPayload>;
  const { form, controls, generalErrors } = useActionEditor<FollowUpFormValues>(
    props,
    followUpToForm(original),
    validateFollowUp,
    (values) => followUpToPayload(values, original),
  );
  // Mantine labels the picker dropdown by the popover's own id, which differs from the input id
  // (unnamed dialog). Sharing one id makes the dropdown's aria-labelledby resolve to the input.
  const dueDateId = useId();
  return (
    <EditorShell editable={props.editable} generalErrors={generalErrors} controls={controls}>
      <TextInput
        label="Title"
        withAsterisk
        description={counter(form.values.title, LIMITS.followUp.title)}
        {...form.getInputProps('title')}
      />
      <Textarea
        label="Description"
        autosize
        minRows={3}
        maxRows={12}
        description={counter(form.values.description, LIMITS.followUp.description)}
        {...form.getInputProps('description')}
      />
      <DateTimePicker
        label="Due date"
        placeholder="No due date"
        clearable
        id={dueDateId}
        popoverProps={{ id: dueDateId }}
        clearButtonProps={DUE_DATE_CLEAR_PROPS}
        ariaLabels={DUE_DATE_ARIA_LABELS}
        submitButtonProps={DUE_DATE_SUBMIT_PROPS}
        valueFormat="MMM D, YYYY HH:mm"
        {...form.getInputProps('due_at')}
        onChange={(value) => form.setFieldValue('due_at', value ? String(value) : null)}
        description={`Stored in UTC. Format ${DATE_TIME_FORMAT}`}
      />
    </EditorShell>
  );
}

// --- No action -----------------------------------------------------------------------------------

export function NoActionEditor(props: EditorProps) {
  const original = props.action.payload as Partial<NoActionPayload>;
  const { form, controls, generalErrors } = useActionEditor<NoActionFormValues>(
    props,
    noActionToForm(original),
    validateNoAction,
    (values) => noActionToPayload(values, original),
  );
  return (
    <EditorShell editable={props.editable} generalErrors={generalErrors} controls={controls}>
      <Textarea
        label="Reason"
        withAsterisk
        autosize
        minRows={3}
        description={counter(form.values.reason, LIMITS.noAction.reason)}
        {...form.getInputProps('reason')}
      />
    </EditorShell>
  );
}

export function ActionEditor(props: EditorProps) {
  switch (props.action.action_type) {
    case 'create_azure_devops_work_item':
      return <AdoEditor {...props} />;
    case 'propose_email_reply':
      return <ReplyEditor {...props} />;
    case 'create_follow_up_task':
      return <FollowUpEditor {...props} />;
    case 'mark_no_action':
      return <NoActionEditor {...props} />;
    default:
      return (
        <Text size="sm" c="dimmed">
          Unsupported action type.
        </Text>
      );
  }
}
