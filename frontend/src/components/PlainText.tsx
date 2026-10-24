import { Box, type BoxProps } from '@mantine/core';

/**
 * Untrusted text (email bodies, AI rationale, drafts). Rendered as a React text node inside a
 * pre-wrap container: no HTML parsing, no link auto-detection, no markdown.
 */
export function PlainText({
  children,
  inline = false,
  ...rest
}: { children: string | null | undefined; inline?: boolean } & BoxProps) {
  return (
    <Box component={inline ? 'span' : 'div'} className="nuvexa-plain-text" {...rest}>
      {children ?? ''}
    </Box>
  );
}
