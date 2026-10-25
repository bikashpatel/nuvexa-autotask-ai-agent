import { Alert, Badge, Blockquote, Code, Group, List, SimpleGrid, Stack, Text } from '@mantine/core';
import { IconAlertTriangle, IconQuote, IconQuestionMark } from '@tabler/icons-react';
import { Fragment } from 'react';

import type { AnalysisOut, ExtractionData } from '../../api/types';
import { ConfidenceIndicator } from '../../components/ConfidenceIndicator';
import { CategoryBadge, PriorityBadge, UrgencyBadge } from '../../components/DomainBadges';
import { FactList } from '../../components/LabeledValue';
import { PlainText } from '../../components/PlainText';
import { SectionCard } from '../../components/SectionCard';
import { formatDateTime, humanize } from '../../lib/format';

function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === 'string' && v.trim() !== '')
    : [];
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function References({ extraction }: { extraction: ExtractionData }) {
  const refs = [
    ...asStringList(extraction.reference_urls),
    ...asStringList(extraction.reference_ids),
    ...asStringList(extraction.references),
  ];
  const unique = Array.from(new Set(refs));
  if (unique.length === 0)
    return (
      <Text span c="dimmed">
        —
      </Text>
    );
  return (
    <Stack gap={4}>
      {unique.map((ref) => (
        // References come from the (untrusted) email: shown as text, never as clickable links.
        <Code key={ref} fz="xs" style={{ wordBreak: 'break-all' }}>
          {ref}
        </Code>
      ))}
      <Text size="xs" c="dimmed">
        Copied verbatim from the email — not clickable.
      </Text>
    </Stack>
  );
}

export function AnalysisPanel({ analysis }: { analysis: AnalysisOut }) {
  const classification = analysis.classification ?? {};
  const extraction = analysis.extraction ?? {};
  const missing = asStringList(classification.missing_information);
  const warnings = Array.from(
    new Set([...analysis.validation_warnings, ...asStringList(classification.extraction_warnings)]),
  );
  const evidence = Array.isArray(extraction.evidence)
    ? extraction.evidence.filter((e) => e && typeof e.quote === 'string')
    : [];
  // Each part is bidi-isolated: a U+202E in the (untrusted) name must not reorder the email after it.
  const requester = [text(extraction.requester_name), text(extraction.requester_email)]
    .filter((part): part is string => Boolean(part))
    .map((part, index) => (
      <Fragment key={part}>
        {index > 0 ? ' · ' : null}
        <bdi>{part}</bdi>
      </Fragment>
    ));

  return (
    <SectionCard
      title="AI analysis"
      id="analysis"
      description={`Attempt #${analysis.attempt} · ${analysis.model_provider} ${analysis.model_name} · prompt ${analysis.prompt_version}`}
    >
      <Stack gap="md">
        <Group gap="xs" wrap="wrap">
          <CategoryBadge category={analysis.category} size="lg" />
          <PriorityBadge priority={analysis.priority} size="lg" />
          <UrgencyBadge urgency={analysis.urgency} size="lg" />
          {analysis.action_recommended ? (
            <Badge variant="light" color="nuvexa" size="lg">
              Action recommended
            </Badge>
          ) : (
            <Badge variant="outline" color="gray" size="lg">
              No action recommended
            </Badge>
          )}
          {analysis.needs_more_info ? (
            <Badge
              variant="light"
              color="yellow"
              size="lg"
              leftSection={<IconQuestionMark size={12} aria-hidden />}
            >
              Needs more info
            </Badge>
          ) : null}
        </Group>

        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
          <ConfidenceIndicator
            score={analysis.confidence_score}
            bucket={analysis.confidence_bucket}
            method={analysis.confidence_method}
          />
          <Stack gap={4}>
            <Text size="sm" fw={600}>
              Rationale
            </Text>
            <PlainText c="dimmed">{text(classification.rationale) ?? '—'}</PlainText>
          </Stack>
        </SimpleGrid>

        {text(extraction.summary) ? (
          <Stack gap={4}>
            <Text size="sm" fw={600}>
              Summary
            </Text>
            <PlainText>{extraction.summary ?? ''}</PlainText>
          </Stack>
        ) : null}

        <Stack gap={6}>
          <Text size="sm" fw={600}>
            Extracted fields
          </Text>
          <FactList
            labelWidth={140}
            items={[
              { label: 'Requester', value: requester.length > 0 ? requester : '—' },
              { label: 'Organization', value: text(extraction.organization) ?? '—' },
              { label: 'Product / system', value: text(extraction.product_or_system) ?? '—' },
              { label: 'Environment', value: text(extraction.environment) ?? '—' },
              { label: 'Impact', value: <PlainText>{text(extraction.impact) ?? '—'}</PlainText> },
              { label: 'Deadline', value: extraction.deadline ? formatDateTime(extraction.deadline) : '—' },
              { label: 'Owner', value: text(extraction.suggested_owner) ?? '—' },
              { label: 'Team', value: text(extraction.suggested_team) ?? '—' },
              {
                label: 'Suggested type',
                value: text(extraction.suggested_work_item_type) ?? '—',
                hidden: !text(extraction.suggested_work_item_type),
              },
              { label: 'References', value: <References extraction={extraction} /> },
            ]}
          />
        </Stack>

        {evidence.length > 0 ? (
          <Stack gap={6}>
            <Text size="sm" fw={600}>
              Evidence
            </Text>
            {evidence.map((item, index) => (
              <Blockquote
                key={`${item.field}-${index}`}
                icon={<IconQuote size={16} aria-hidden />}
                iconSize={28}
                p="sm"
                pl="lg"
                color="gray"
                cite={`— supports ${humanize(item.field)}`}
              >
                <PlainText>{item.quote}</PlainText>
              </Blockquote>
            ))}
          </Stack>
        ) : null}

        {missing.length > 0 ? (
          <Alert
            color="yellow"
            variant="light"
            title="Missing information"
            icon={<IconQuestionMark size={18} aria-hidden />}
          >
            <List size="sm">
              {missing.map((item) => (
                <List.Item key={item}>{item}</List.Item>
              ))}
            </List>
          </Alert>
        ) : null}

        {warnings.length > 0 ? (
          <Alert
            color="orange"
            variant="light"
            title="Validation warnings"
            icon={<IconAlertTriangle size={18} aria-hidden />}
          >
            <List size="sm">
              {warnings.map((item) => (
                <List.Item key={item}>{item}</List.Item>
              ))}
            </List>
          </Alert>
        ) : null}

        <Text size="xs" c="dimmed">
          Schema {analysis.schema_version} · analyzed {formatDateTime(analysis.created_at)}. NUVEXA stores
          concise rationales and evidence only — never chain-of-thought.
        </Text>
      </Stack>
    </SectionCard>
  );
}
