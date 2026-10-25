import type { WorkflowEventOut, WorkflowRunOut, WorkflowStatus } from '../../api/types';
import { lookup } from '../../lib/lookup';

/** The happy path shown by the stepper. */
export const WORKFLOW_STEPS: { status: WorkflowStatus; label: string; description: string }[] = [
  { status: 'received', label: 'Received', description: 'Email ingested' },
  { status: 'normalized', label: 'Normalized', description: 'Sanitized + injection screening' },
  { status: 'classified', label: 'Classified', description: 'Category, priority, urgency' },
  { status: 'extracted', label: 'Extracted', description: 'Structured fields + evidence' },
  { status: 'validated', label: 'Validated', description: 'Grounding, schema, confidence' },
  { status: 'action_proposed', label: 'Action proposed', description: 'Policy-checked proposals' },
  { status: 'pending_approval', label: 'Pending approval', description: 'Waiting for a human' },
  { status: 'approved', label: 'Approved', description: 'Bound to version + hash' },
  { status: 'executing', label: 'Executing', description: 'Deterministic adapter write' },
  { status: 'completed', label: 'Completed', description: 'Done' },
];

const STEP_INDEX = new Map(WORKFLOW_STEPS.map((s, i) => [s.status, i]));

/** Graph node → index of the status that node produces (used to place failures). */
const NODE_TO_INDEX: Record<string, number> = {
  received: 0,
  ingest: 0,
  load_email: 1,
  normalize: 1,
  classify: 2,
  extract: 3,
  validate: 4,
  plan: 5,
  propose: 5,
  await_review: 6,
  review: 6,
  approve: 7,
  execute: 8,
  executing: 8,
};

export type TerminalKind = 'rejected' | 'failed' | 'cancelled';

export interface StepperState {
  /** Mantine Stepper `active`: steps before it render completed. */
  active: number;
  /** Step currently being processed by the worker (shows a loader). */
  loadingIndex: number | null;
  /** Step where the run stopped for a terminal-negative outcome. */
  stoppedIndex: number | null;
  terminal: TerminalKind | null;
}

function failureIndex(run: WorkflowRunOut, events: WorkflowEventOut[]): number {
  const fromStep = lookup(NODE_TO_INDEX, run.current_step);
  if (fromStep !== undefined) return fromStep;
  const failed = [...events].reverse().find((e) => e.event_type === 'step_failed');
  return lookup(NODE_TO_INDEX, failed?.step) ?? 0;
}

export function computeStepperState(run: WorkflowRunOut, events: WorkflowEventOut[] = []): StepperState {
  const status = run.status;
  switch (status) {
    case 'received':
    case 'normalized':
    case 'classified':
    case 'extracted':
    case 'validated':
    case 'action_proposed': {
      const index = (STEP_INDEX.get(status) ?? 0) + 1;
      return { active: index, loadingIndex: index, stoppedIndex: null, terminal: null };
    }
    case 'pending_approval':
      return { active: 6, loadingIndex: null, stoppedIndex: null, terminal: null };
    case 'approved':
      return { active: 8, loadingIndex: null, stoppedIndex: null, terminal: null };
    case 'executing':
      return { active: 8, loadingIndex: 8, stoppedIndex: null, terminal: null };
    case 'completed':
      return { active: WORKFLOW_STEPS.length, loadingIndex: null, stoppedIndex: null, terminal: null };
    case 'rejected':
      return { active: 6, loadingIndex: null, stoppedIndex: 6, terminal: 'rejected' };
    case 'cancelled': {
      const index = lookup(NODE_TO_INDEX, run.current_step) ?? 6;
      return { active: index, loadingIndex: null, stoppedIndex: index, terminal: 'cancelled' };
    }
    case 'failed': {
      const index = failureIndex(run, events);
      return { active: index, loadingIndex: null, stoppedIndex: index, terminal: 'failed' };
    }
    default:
      return { active: 0, loadingIndex: null, stoppedIndex: null, terminal: null };
  }
}

// --- Event detail extraction (details are sanitized but loosely typed) ---------------------------

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function obj(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export interface EventFacts {
  model: string | null;
  provider: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
  costUsd: number | null;
  toolName: string | null;
  toolArgs: unknown;
  retryCount: number | null;
  /** LLM call attempts (provider-level retries) when > 1. */
  attempts: number | null;
  error: string | null;
  errorCategory: string | null;
  promptVersion: string | null;
}

export function extractEventFacts(details: Record<string, unknown>): EventFacts {
  const usage = obj(details.usage) ?? obj(details.token_usage) ?? {};
  const inputTokens = num(usage.input_tokens) ?? num(usage.prompt_tokens) ?? num(details.input_tokens);
  const outputTokens = num(usage.output_tokens) ?? num(usage.completion_tokens) ?? num(details.output_tokens);
  const totalTokens =
    num(usage.total_tokens) ??
    num(details.total_tokens) ??
    (inputTokens !== null || outputTokens !== null ? (inputTokens ?? 0) + (outputTokens ?? 0) : null);
  const errorObj = obj(details.error);
  return {
    model: str(details.model) ?? str(details.model_name) ?? str(usage.model),
    provider: str(details.provider) ?? str(details.model_provider) ?? str(usage.provider),
    inputTokens,
    outputTokens,
    totalTokens,
    costUsd: num(details.estimated_cost_usd) ?? num(usage.estimated_cost_usd),
    toolName: str(details.tool_name) ?? str(details.tool),
    toolArgs: details.arguments ?? details.args ?? details.sanitized_args ?? details.tool_args ?? null,
    retryCount: num(details.retry_count) ?? num(details.retries),
    attempts: (() => {
      const attempts = num(usage.attempts);
      return attempts !== null && attempts > 1 ? attempts : null;
    })(),
    error:
      str(details.error) ?? str(errorObj?.message) ?? str(details.error_message) ?? str(details.error_code),
    errorCategory: str(details.error_category) ?? str(details.failure_category) ?? str(errorObj?.category),
    promptVersion: str(details.prompt_version),
  };
}
