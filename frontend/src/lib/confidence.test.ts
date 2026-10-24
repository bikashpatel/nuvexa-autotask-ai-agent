import { describe, expect, it } from 'vitest';

import { confidenceTooltip } from './confidence';

describe('confidenceTooltip', () => {
  it('names the method and says the score is not a calibrated probability', () => {
    expect(confidenceTooltip('heuristic-v2')).toBe(
      'Heuristic estimate, not a calibrated probability; method heuristic-v2.',
    );
  });

  it.each([null, undefined, ''])('defaults the method to heuristic-v1 for %j', (method) => {
    expect(confidenceTooltip(method)).toBe(
      'Heuristic estimate, not a calibrated probability; method heuristic-v1.',
    );
  });
});
