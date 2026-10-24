export type LinkKind = 'http' | 'mock' | 'unsafe';

/**
 * Only http(s) URLs may become links. `mock://` (mock Azure DevOps) is shown as text with a badge;
 * every other scheme (javascript:, data:, file:, …) is treated as unsafe and rendered as text.
 */
export function classifyUrl(url: string): LinkKind {
  const value = url.trim().toLowerCase();
  if (value.startsWith('mock://')) return 'mock';
  if (value.startsWith('https://') || value.startsWith('http://')) return 'http';
  return 'unsafe';
}
