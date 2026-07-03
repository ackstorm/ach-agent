import { fireEvent, render, screen } from '@testing-library/react';
import { expect, test, vi } from 'vitest';

import { RangePresets } from './RangePresets';

test('clicking a preset calls onChange with its days value', () => {
  const onChange = vi.fn();
  render(<RangePresets days={30} onChange={onChange} />);
  fireEvent.click(screen.getByText('60d'));
  expect(onChange).toHaveBeenCalledWith(60);
});
