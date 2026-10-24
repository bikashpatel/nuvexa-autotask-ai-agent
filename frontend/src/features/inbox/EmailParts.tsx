import { Alert, Card, Text } from '@mantine/core';
import { IconArchive } from '@tabler/icons-react';

import type { EmailDetail, RecipientOut } from '../../api/types';
import { ExternalLink } from '../../components/ExternalLink';
import { FactList } from '../../components/LabeledValue';
import { PlainText } from '../../components/PlainText';
import { formatDateTime, humanize, personLabel } from '../../lib/format';

function recipients(list: RecipientOut[]): string {
  if (list.length === 0) return '—';
  return list.map((r) => personLabel(r.name, r.address)).join(', ');
}

export function EmailHeaders({ email }: { email: EmailDetail }) {
  return (
    <FactList
      items={[
        { label: 'From', value: personLabel(email.sender_name, email.sender_address) },
        { label: 'To', value: recipients(email.to_recipients) },
        { label: 'Cc', value: recipients(email.cc_recipients), hidden: email.cc_recipients.length === 0 },
        { label: 'Received', value: formatDateTime(email.received_at) },
        { label: 'Importance', value: humanize(email.importance), hidden: !email.importance },
        {
          label: 'Message ID',
          value: (
            <Text size="xs" ff="monospace" style={{ overflowWrap: 'anywhere' }}>
              {email.internet_message_id ?? '—'}
            </Text>
          ),
          hidden: !email.internet_message_id,
        },
        {
          label: 'Open in Outlook',
          value: <ExternalLink url={email.web_link}>Outlook on the web</ExternalLink>,
          hidden: !email.web_link,
        },
      ]}
    />
  );
}

export function EmailBody({ email }: { email: EmailDetail }) {
  if (email.body_purged) {
    return (
      <Alert
        color="gray"
        variant="light"
        icon={<IconArchive size={18} aria-hidden />}
        title="Body purged by retention policy"
      >
        The message body was removed after the tenant retention period. Metadata and analysis results are
        kept.
      </Alert>
    );
  }
  return (
    <Card withBorder padding="md" bg="var(--mantine-color-body)">
      <PlainText>{email.body_text || email.body_preview || '(empty body)'}</PlainText>
    </Card>
  );
}
