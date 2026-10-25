import { useDebouncedValue } from '@mantine/hooks';
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router';

/**
 * A free-text list filter whose source of truth is one URL query parameter.
 *
 * * The box pushes its debounced, trimmed value into the URL (`replace`, resetting `offset`). Only a
 *   *new* debounced value is pushed: comparing against the URL alone would re-apply a stale value
 *   whenever the URL changes first (e.g. "Clear filters" before the debounce settles).
 * * The box follows the URL whenever the parameter changes to a value the box did not push itself —
 *   browser Back/Forward, or an in-app link to the mounted page (the sidebar "Inbox" link while
 *   filtered) — so it always shows the filter the list is actually using. The box's own pushes are
 *   not echoed back, so typing that continues while a push lands is never overwritten.
 *
 * All bookkeeping is state adjusted during render (no refs read in render, no setState in effects).
 */
export function useUrlTextFilter(key: string, delayMs = 350) {
  const [params, setParams] = useSearchParams();
  const urlValue = params.get(key) ?? '';
  const [value, setValue] = useState(urlValue);
  const [debounced] = useDebouncedValue(value.trim(), delayMs);

  // The value the box wants in the URL: its latest debounced input, or the URL value it adopted.
  const [target, setTarget] = useState(urlValue);
  const [seenDebounced, setSeenDebounced] = useState(debounced);
  const [seenUrlValue, setSeenUrlValue] = useState(urlValue);

  if (debounced !== seenDebounced) {
    setSeenDebounced(debounced);
    setTarget(debounced);
  }
  if (urlValue !== seenUrlValue) {
    setSeenUrlValue(urlValue);
    if (urlValue !== target) {
      setValue(urlValue);
      setTarget(urlValue);
    }
  }

  useEffect(() => {
    if (target === urlValue) return;
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (target) next.set(key, target);
        else next.delete(key);
        next.delete('offset');
        return next;
      },
      { replace: true },
    );
  }, [key, target, urlValue, setParams]);

  return [value, setValue] as const;
}
