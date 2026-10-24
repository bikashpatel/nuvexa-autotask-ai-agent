import { describe, expect, it } from 'vitest';

import { WORKFLOW_STATUSES, type WorkflowStatus } from '../../api/types';
import { workflowEvent, workflowRun } from '../../test/fixtures';
import { WORKFLOW_STEPS, computeStepperState, extractEventFacts } from './workflowSteps';

describe('WORKFLOW_STEPS', () => {
  it('is the happy path from received to completed, each with a label and description', () => {
    expect(WORKFLOW_STEPS.map((s) => s.status)).toEqual([
      'received',
      'normalized',
      'classified',
      'extracted',
      'validated',
      'action_proposed',
      'pending_approval',
      'approved',
      'executing',
      'completed',
    ]);
    for (const step of WORKFLOW_STEPS) {
      expect(step.label).toBeTruthy();
      expect(step.description).toBeTruthy();
    }
  });
});

describe('computeStepperState', () => {
  const expected: Record<WorkflowStatus, ReturnType<typeof computeStepperState>> = {
    received: { active: 1, loadingIndex: 1, stoppedIndex: null, terminal: null },
    normalized: { active: 2, loadingIndex: 2, stoppedIndex: null, terminal: null },
    classified: { active: 3, loadingIndex: 3, stoppedIndex: null, terminal: null },
    extracted: { active: 4, loadingIndex: 4, stoppedIndex: null, terminal: null },
    validated: { active: 5, loadingIndex: 5, stoppedIndex: null, terminal: null },
    action_proposed: { active: 6, loadingIndex: 6, stoppedIndex: null, terminal: null },
    pending_approval: { active: 6, loadingIndex: null, stoppedIndex: null, terminal: null },
    approved: { active: 8, loadingIndex: null, stoppedIndex: null, terminal: null },
    executing: { active: 8, loadingIndex: 8, stoppedIndex: null, terminal: null },
    completed: { active: 10, loadingIndex: null, stoppedIndex: null, terminal: null },
    rejected: { active: 6, loadingIndex: null, stoppedIndex: 6, terminal: 'rejected' },
    // current_step 'await_review' (fixture default) maps to index 6.
    cancelled: { active: 6, loadingIndex: null, stoppedIndex: 6, terminal: 'cancelled' },
    failed: { active: 6, loadingIndex: null, stoppedIndex: 6, terminal: 'failed' },
  };

  it.each(WORKFLOW_STATUSES.map((s) => [s] as const))('maps %s', (status) => {
    expect(computeStepperState(workflowRun({ status }))).toEqual(expected[status]);
  });

  it('falls back to an empty stepper for unknown (forward-compatible) statuses', () => {
    expect(computeStepperState(workflowRun({ status: 'brand_new' as WorkflowStatus }))).toEqual({
      active: 0,
      loadingIndex: null,
      stoppedIndex: null,
      terminal: null,
    });
  });

  it('places a cancelled run at its current step, or at review when unknown/missing', () => {
    expect(computeStepperState(workflowRun({ status: 'cancelled', current_step: 'classify' }))).toMatchObject(
      {
        active: 2,
        stoppedIndex: 2,
        terminal: 'cancelled',
      },
    );
    expect(computeStepperState(workflowRun({ status: 'cancelled', current_step: null }))).toMatchObject({
      stoppedIndex: 6,
    });
    expect(computeStepperState(workflowRun({ status: 'cancelled', current_step: 'mystery' }))).toMatchObject({
      stoppedIndex: 6,
    });
  });

  it('places a failed run at the current step when it is a known node', () => {
    expect(computeStepperState(workflowRun({ status: 'failed', current_step: 'extract' }))).toEqual({
      active: 3,
      loadingIndex: null,
      stoppedIndex: 3,
      terminal: 'failed',
    });
    expect(computeStepperState(workflowRun({ status: 'failed', current_step: 'execute' })).stoppedIndex).toBe(
      8,
    );
  });

  it('otherwise uses the most recent step_failed event', () => {
    const events = [
      workflowEvent({ id: 'e1', step: 'classify', event_type: 'step_failed' }),
      workflowEvent({ id: 'e2', step: 'validate', event_type: 'step_failed' }),
      workflowEvent({ id: 'e3', step: 'plan', event_type: 'step_completed' }),
    ];
    const run = workflowRun({ status: 'failed', current_step: null });
    expect(computeStepperState(run, events).stoppedIndex).toBe(4);
    // Unknown current_step also falls through to the events.
    expect(
      computeStepperState(workflowRun({ status: 'failed', current_step: '??' }), events).stoppedIndex,
    ).toBe(4);
  });

  it('falls back to index 0 when neither the step nor the events identify the failure', () => {
    const run = workflowRun({ status: 'failed', current_step: null });
    expect(computeStepperState(run).stoppedIndex).toBe(0);
    expect(
      computeStepperState(run, [workflowEvent({ step: 'mystery', event_type: 'step_failed' })]).stoppedIndex,
    ).toBe(0);
  });
});

describe('extractEventFacts', () => {
  it('returns nulls for empty details', () => {
    expect(extractEventFacts({})).toEqual({
      model: null,
      provider: null,
      inputTokens: null,
      outputTokens: null,
      totalTokens: null,
      costUsd: null,
      toolName: null,
      toolArgs: null,
      retryCount: null,
      attempts: null,
      error: null,
      errorCategory: null,
      promptVersion: null,
    });
  });

  it('reads `usage` with input/output tokens and computes the total', () => {
    const facts = extractEventFacts({
      model: 'gpt-x',
      provider: 'openai',
      usage: { input_tokens: 800, output_tokens: 120 },
      estimated_cost_usd: 0.0123,
      prompt_version: 'p7',
    });
    expect(facts).toMatchObject({
      model: 'gpt-x',
      provider: 'openai',
      inputTokens: 800,
      outputTokens: 120,
      totalTokens: 920,
      costUsd: 0.0123,
      promptVersion: 'p7',
    });
  });

  it('accepts token_usage with prompt/completion aliases, an explicit total and usage-level model/provider/cost', () => {
    const facts = extractEventFacts({
      token_usage: {
        prompt_tokens: 10,
        completion_tokens: 5,
        total_tokens: 99,
        model: 'm',
        provider: 'p',
        estimated_cost_usd: 1.5,
      },
    });
    expect(facts).toMatchObject({
      inputTokens: 10,
      outputTokens: 5,
      totalTokens: 99,
      model: 'm',
      provider: 'p',
      costUsd: 1.5,
    });
  });

  it('accepts details-level tokens and model_name/model_provider aliases', () => {
    expect(
      extractEventFacts({ input_tokens: 3, output_tokens: 4, model_name: 'mn', model_provider: 'mp' }),
    ).toMatchObject({ inputTokens: 3, outputTokens: 4, totalTokens: 7, model: 'mn', provider: 'mp' });
    expect(extractEventFacts({ total_tokens: 50 })).toMatchObject({ totalTokens: 50, inputTokens: null });
    // Only one side known: the other counts as 0.
    expect(extractEventFacts({ output_tokens: 6 }).totalTokens).toBe(6);
    expect(extractEventFacts({ input_tokens: 5 }).totalTokens).toBe(5);
  });

  it('reads tool name and arguments through every alias', () => {
    expect(extractEventFacts({ tool_name: 't1', arguments: { a: 1 } })).toMatchObject({
      toolName: 't1',
      toolArgs: { a: 1 },
    });
    expect(extractEventFacts({ tool: 't2', args: [1] })).toMatchObject({ toolName: 't2', toolArgs: [1] });
    expect(extractEventFacts({ sanitized_args: { s: true } }).toolArgs).toEqual({ s: true });
    expect(extractEventFacts({ tool_args: 'raw' }).toolArgs).toBe('raw');
  });

  it('reads retries, and attempts only when greater than one', () => {
    expect(extractEventFacts({ retry_count: 2 }).retryCount).toBe(2);
    expect(extractEventFacts({ retries: 1 }).retryCount).toBe(1);
    expect(extractEventFacts({ usage: { attempts: 3 } }).attempts).toBe(3);
    expect(extractEventFacts({ usage: { attempts: 1 } }).attempts).toBeNull();
  });

  it('reads the error from a string, an error object, error_message or error_code', () => {
    expect(extractEventFacts({ error: 'boom' }).error).toBe('boom');
    expect(extractEventFacts({ error: { message: 'nested', category: 'timeout' } })).toMatchObject({
      error: 'nested',
      errorCategory: 'timeout',
    });
    expect(extractEventFacts({ error_message: 'msg' }).error).toBe('msg');
    expect(extractEventFacts({ error_code: 'E42' }).error).toBe('E42');
    expect(extractEventFacts({ error_category: 'transient' }).errorCategory).toBe('transient');
    expect(extractEventFacts({ failure_category: 'llm_output' }).errorCategory).toBe('llm_output');
  });

  it('ignores non-finite numbers, wrong types, blank strings and array-shaped objects', () => {
    const facts = extractEventFacts({
      model: '   ',
      provider: 42,
      usage: [1, 2, 3],
      input_tokens: Number.NaN,
      output_tokens: Number.POSITIVE_INFINITY,
      estimated_cost_usd: '0.5',
      retry_count: '2',
      error: { message: '' },
      prompt_version: '',
    });
    expect(facts).toMatchObject({
      model: null,
      provider: null,
      inputTokens: null,
      outputTokens: null,
      totalTokens: null,
      costUsd: null,
      retryCount: null,
      error: null,
      promptVersion: null,
    });
  });
});
