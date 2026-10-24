import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Modal,
  Paper,
  Table,
  Tooltip,
  createTheme,
  type MantineColorsTuple,
} from '@mantine/core';

/** NUVEXA brand: an indigo-violet primary, deliberately distinct from the "approved" status blue. */
const nuvexa: MantineColorsTuple = [
  '#f1efff',
  '#dfdcfd',
  '#bcb4f7',
  '#978af2',
  '#7867ee',
  '#6551eb',
  '#5b45eb',
  '#4b37d1',
  '#4130bc',
  '#3527a6',
];

/** Secondary accent used for brand gradients only. */
const lagoon: MantineColorsTuple = [
  '#e3fbfc',
  '#d3f2f4',
  '#abe3e7',
  '#7ed4da',
  '#5ac7cf',
  '#43bfc8',
  '#33bbc5',
  '#20a4ad',
  '#07929b',
  '#007f87',
];

export const theme = createTheme({
  primaryColor: 'nuvexa',
  primaryShade: { light: 6, dark: 4 },
  colors: { nuvexa, lagoon },
  defaultRadius: 'md',
  cursorType: 'pointer',
  focusRing: 'auto',
  fontFamily:
    'Inter, "Segoe UI Variable", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Roboto, "Helvetica Neue", Arial, sans-serif',
  fontFamilyMonospace: 'ui-monospace, "Cascadia Code", SFMono-Regular, Menlo, Consolas, monospace',
  headings: {
    fontWeight: '650',
    sizes: {
      h1: { fontSize: '1.75rem', lineHeight: '1.25' },
      h2: { fontSize: '1.375rem', lineHeight: '1.3' },
      h3: { fontSize: '1.125rem', lineHeight: '1.35' },
      h4: { fontSize: '1rem', lineHeight: '1.4' },
    },
  },
  components: {
    Card: Card.extend({ defaultProps: { withBorder: true, radius: 'md', padding: 'lg' } }),
    Paper: Paper.extend({ defaultProps: { radius: 'md' } }),
    Button: Button.extend({ defaultProps: { radius: 'md' } }),
    ActionIcon: ActionIcon.extend({ defaultProps: { variant: 'subtle', radius: 'md' } }),
    Badge: Badge.extend({ defaultProps: { radius: 'sm', tt: 'none' } }),
    Tooltip: Tooltip.extend({ defaultProps: { withArrow: true, openDelay: 150, multiline: true, maw: 320 } }),
    Modal: Modal.extend({ defaultProps: { centered: true, radius: 'md' } }),
    Alert: Alert.extend({ defaultProps: { radius: 'md' } }),
    Table: Table.extend({ defaultProps: { verticalSpacing: 'sm', highlightOnHover: true } }),
  },
});
