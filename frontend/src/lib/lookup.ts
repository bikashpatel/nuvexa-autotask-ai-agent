/**
 * Own-key lookups in constant maps keyed by an API enum (labels, icons, colours).
 *
 * API values are open strings to the browser — forward-compatible additions, or a hostile value — so
 * `MAP[value]` must not resolve an inherited name: "constructor" is `Object`, "__proto__" is
 * `Object.prototype`, "toString" is a function. Anything that is not an own key is treated exactly
 * like an unknown value (same rule as `getStatusDescriptor` in theme/status.ts).
 */

/** Type guard: `key` is an own key of `map` (never an inherited Object.prototype member). */
export function isOwnKey<M extends object>(map: M, key: PropertyKey | null | undefined): key is keyof M {
  return key !== null && key !== undefined && Object.hasOwn(map, key);
}

/** The own value for `key`, or `undefined` (callers fall back as for an unknown value). */
export function lookup<V>(
  map: Readonly<Record<string | number, V>>,
  key: string | number | null | undefined,
): V | undefined {
  return isOwnKey(map, key) ? map[key] : undefined;
}
