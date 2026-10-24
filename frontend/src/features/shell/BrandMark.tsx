import { Box } from '@mantine/core';

/** NUVEXA logomark (decorative). */
export function BrandMark({ size = 32 }: { size?: number }) {
  return (
    <Box
      className="nuvexa-brand-mark"
      w={size}
      h={size}
      style={{ borderRadius: size * 0.25, display: 'grid', placeItems: 'center', flexShrink: 0 }}
      aria-hidden
    >
      <svg width={size * 0.56} height={size * 0.56} viewBox="0 0 28 28" fill="none">
        <path d="M5 23V5h3.6L19.4 17V5H23v18h-3.6L8.6 11v12z" fill="currentColor" />
      </svg>
    </Box>
  );
}
