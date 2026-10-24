import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ACTION_STATUSES, WORKFLOW_STATUSES } from '../api/types';
import { StatusBadge } from '../components/StatusBadge';
import { renderWithProviders } from '../test/utils';
import {
  ACTION_STATUS,
  WORKFLOW_STATUS,
  getStatusDescriptor,
  isActionInFlight,
  isWorkflowInFlight,
} from './status';

describe('status mapping', () => {
  it('maps every action status to the agreed colour language', () => {
    expect(ACTION_STATUS.pending_approval).toMatchObject({ color: 'orange', label: 'Pending approval' });
    expect(ACTION_STATUS.approved).toMatchObject({ color: 'blue', label: 'Approved' });
    expect(ACTION_STATUS.executing).toMatchObject({ color: 'cyan', loading: true });
    expect(ACTION_STATUS.completed).toMatchObject({ color: 'green' });
    expect(ACTION_STATUS.failed).toMatchObject({ color: 'red', variant: 'light' });
    expect(ACTION_STATUS.uncertain).toMatchObject({ color: 'grape' });
    expect(ACTION_STATUS.rejected).toMatchObject({ color: 'red', variant: 'outline' });
    expect(ACTION_STATUS.cancelled).toMatchObject({ color: 'gray' });
    expect(ACTION_STATUS.superseded).toMatchObject({ color: 'gray', dimmed: true });
  });

  it('has a label and icon for every enum value (colour is never the only signal)', () => {
    for (const status of ACTION_STATUSES) {
      expect(ACTION_STATUS[status].label).toBeTruthy();
      expect(ACTION_STATUS[status].icon).toBeTruthy();
    }
    for (const status of WORKFLOW_STATUSES) {
      expect(WORKFLOW_STATUS[status].label).toBeTruthy();
    }
  });

  it('treats received…action_proposed and executing as in-flight (polling)', () => {
    const inFlight = WORKFLOW_STATUSES.filter((s) => isWorkflowInFlight(s));
    expect(inFlight).toEqual([
      'received',
      'normalized',
      'classified',
      'extracted',
      'validated',
      'action_proposed',
      'executing',
    ]);
    expect(isActionInFlight('executing')).toBe(true);
    expect(isActionInFlight('approved')).toBe(false);
    expect(isWorkflowInFlight('pending_approval')).toBe(false);
  });

  it('falls back to a neutral, humanized descriptor for unknown values', () => {
    expect(getStatusDescriptor('action', 'brand_new_state')).toMatchObject({
      label: 'Brand new state',
      tone: 'neutral',
      color: 'gray',
    });
  });
});

describe('<StatusBadge>', () => {
  it('renders the label, an accessible name and the tone', () => {
    renderWithProviders(<StatusBadge kind="action" status="uncertain" />);
    const badge = screen.getByLabelText('Status: Uncertain');
    expect(badge).toHaveTextContent('Uncertain');
    expect(badge).toHaveAttribute('data-tone', 'uncertain');
    expect(badge).toHaveAttribute('data-status', 'uncertain');
  });

  it('renders a loader for executing', () => {
    const { container } = renderWithProviders(<StatusBadge kind="action" status="executing" />);
    expect(screen.getByLabelText('Status: Executing')).toBeInTheDocument();
    expect(container.querySelector('.mantine-Loader-root')).not.toBeNull();
  });
});
