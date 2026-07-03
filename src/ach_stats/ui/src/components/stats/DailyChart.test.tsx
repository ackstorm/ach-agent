import { render, screen } from '@testing-library/react';
import { expect, test } from 'vitest';

import { DailyChart } from './DailyChart';

test('renders empty state with no series data', () => {
  render(<DailyChart series={[]} metric="spend" />);
  expect(screen.getByText(/no usage in this range/i)).toBeInTheDocument();
});

test('renders a chart with series data', () => {
  const series = [
    { date: '2026-06-01', spend: 1.5, sessions: 2, tokens: 100, partial: false },
    { date: '2026-06-02', spend: 2.5, sessions: 3, tokens: 200, partial: false },
  ];
  const { container } = render(<DailyChart series={series} metric="sessions" />);
  expect(container.querySelector('.recharts-responsive-container')).not.toBeNull();
});
