import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import Leaderboard from './Leaderboard';

afterEach(() => vi.restoreAllMocks());

function renderPage(contract: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(contract), { status: 200 })));
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <Leaderboard />
    </QueryClientProvider>,
  );
}

const base = {
  range: { start: 0, end: 1, days: 30, coverage_start: null, tz: 'UTC' },
  totals: { sessions: 3, tokens: 100, spend: 1.0, avg_cost_per_session: 0.33, aborted: 1, partial: false },
  leaderboard: {
    sorted_by: 'spend',
    rows: [
      { rank: 1, model: 'claude-opus-4-8', provider: 'Anthropic', tag: 'Frontier', score: null,
        speed_tok_s: 63, cost_per_mtok: 31.5, spend: 0.9, sessions: 2 },
    ],
  },
  cost_per_session: [], sessions_this_month: { rows: [], partial: false }, series: [], recent: [],
};

test('renders ranked model and unrated score', async () => {
  renderPage(base);
  await waitFor(() => expect(screen.getByText('claude-opus-4-8')).toBeInTheDocument());
  expect(screen.getByText(/unrated/i)).toBeInTheDocument();
});

test('renders the partial banner when totals.partial', async () => {
  renderPage({ ...base, totals: { ...base.totals, partial: true },
               range: { ...base.range, coverage_start: 1_700_000_000_000 } });
  await waitFor(() => expect(screen.getByText(/showing data since/i)).toBeInTheDocument());
});
