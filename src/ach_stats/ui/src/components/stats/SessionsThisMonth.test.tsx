import { render, screen } from '@testing-library/react';
import { expect, test } from 'vitest';

import { SessionsThisMonth } from './SessionsThisMonth';

test('renders empty-state copy with no rows', () => {
  render(<SessionsThisMonth data={{ rows: [], partial: false }} />);
  expect(screen.getByText(/no sessions yet this month/i)).toBeInTheDocument();
});

test('renders model counts and the partial note', () => {
  render(
    <SessionsThisMonth
      data={{ rows: [{ model: 'claude-opus-4-8', count: 5 }], partial: true }}
    />,
  );
  expect(screen.getByText('claude-opus-4-8')).toBeInTheDocument();
  expect(screen.getByText('5')).toBeInTheDocument();
  expect(screen.getByText(/partial/i)).toBeInTheDocument();
});
