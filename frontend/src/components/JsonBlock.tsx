import { Code, ScrollArea } from '@mantine/core';

/** Pretty-printed JSON rendered as text (never as HTML). */
export function JsonBlock({ value, maxHeight = 320 }: { value: unknown; maxHeight?: number }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? 'null';
  } catch {
    text = String(value);
  }
  return (
    <ScrollArea.Autosize mah={maxHeight} type="auto">
      <Code block fz="xs" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        {text}
      </Code>
    </ScrollArea.Autosize>
  );
}
