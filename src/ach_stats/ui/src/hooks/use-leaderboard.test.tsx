import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import { useLeaderboard } from './use-leaderboard';

afterEach(() => vi.restoreAllMocks());

test('useLeaderboard fetches and returns the contract', async () => {
  const contract = { leaderboard: { sorted_by: 'spend', rows: [] }, totals: { sessions: 0 } };
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(contract), { status: 200 })));

  const qc = new QueryClient();
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useLeaderboard(30), { wrapper });
  await waitFor(() => expect(result.current.data).toBeDefined());
  expect(result.current.data!.leaderboard.sorted_by).toBe('spend');
});
