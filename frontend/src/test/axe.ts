/**
 * axe-core accessibility check for rendered pages. Only `serious` and `critical` violations fail.
 *
 * Disabled rules (jsdom limitations, not product decisions):
 *  - color-contrast: jsdom does no layout or style resolution, so contrast cannot be computed.
 * Run with real timers: axe schedules work with timers internally.
 */
import axe from 'axe-core';
import { expect } from 'vitest';

export interface A11yViolationSummary {
  id: string;
  impact: string | null | undefined;
  help: string;
  targets: string[];
}

export async function seriousA11yViolations(
  context: Element = document.body,
): Promise<A11yViolationSummary[]> {
  const results = await axe.run(context, {
    resultTypes: ['violations'],
    rules: { 'color-contrast': { enabled: false } },
  });
  return results.violations
    .filter((v) => v.impact === 'serious' || v.impact === 'critical')
    .map((v) => ({
      id: v.id,
      impact: v.impact,
      help: v.help,
      targets: v.nodes.map((n) => n.target.join(' ')),
    }));
}

export async function expectNoSeriousA11yViolations(context: Element = document.body): Promise<void> {
  expect(await seriousA11yViolations(context)).toEqual([]);
}
