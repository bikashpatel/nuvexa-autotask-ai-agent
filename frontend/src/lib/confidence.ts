/** Tooltip text for the heuristic confidence score (see docs/architecture.md § Confidence). */
export function confidenceTooltip(method: string | null | undefined): string {
  return `Heuristic estimate, not a calibrated probability; method ${method || 'heuristic-v1'}.`;
}
