import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { expectNoSeriousA11yViolations } from '../test/axe';
import { renderWithProviders } from '../test/utils';
import { FactList } from './LabeledValue';

describe('<FactList>', () => {
  it('renders a definition list pairing each label (dt) with its value (dd)', async () => {
    const { container } = renderWithProviders(
      <FactList
        items={[
          { label: 'From', value: 'Dana Customer <dana@fabrikam.example>' },
          { label: 'Attempt', value: <strong>#2</strong> },
        ]}
      />,
    );
    const dl = container.querySelector('dl')!;
    expect(dl).toHaveAttribute('data-orientation', 'horizontal');
    const terms = screen.getAllByRole('term');
    const definitions = screen.getAllByRole('definition');
    expect(terms.map((t) => t.textContent)).toEqual(['From', 'Attempt']);
    expect(definitions.map((d) => d.textContent)).toEqual(['Dana Customer <dana@fabrikam.example>', '#2']);
    expect(terms[0]!.nextElementSibling).toBe(definitions[0]);
    expect(terms[1]!.nextElementSibling).toBe(definitions[1]);
    expect(terms.every((t) => dl.contains(t))).toBe(true);
    await expectNoSeriousA11yViolations();
  });

  it('omits hidden items', () => {
    renderWithProviders(
      <FactList
        items={[
          { label: 'Cc', value: 'nobody', hidden: true },
          { label: 'To', value: 'Support' },
          { label: 'Importance', value: 'High', hidden: false },
        ]}
      />,
    );
    expect(screen.queryByText('Cc')).toBeNull();
    expect(screen.queryByText('nobody')).toBeNull();
    expect(screen.getAllByRole('term').map((t) => t.textContent)).toEqual(['To', 'Importance']);
  });

  it('supports the vertical orientation', () => {
    const { container } = renderWithProviders(
      <FactList orientation="vertical" labelWidth="10rem" items={[{ label: 'A', value: '1' }]} />,
    );
    expect(container.querySelector('dl')).toHaveAttribute('data-orientation', 'vertical');
  });

  it('renders an empty list without items', () => {
    const { container } = renderWithProviders(<FactList items={[]} />);
    expect(container.querySelector('dl')).toBeEmptyDOMElement();
  });
});
