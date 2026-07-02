import { useQuery } from '@tanstack/react-query';

import type { Contract } from '@/lib/api-types';

async function fetchLeaderboard(days: number): Promise<Contract> {
  const res = await fetch(`/api/leaderboard?days=${days}`);
  if (!res.ok) throw new Error(`leaderboard ${res.status}`);
  return (await res.json()) as Contract;
}

export function useLeaderboard(days: number) {
  return useQuery({ queryKey: ['leaderboard', days], queryFn: () => fetchLeaderboard(days) });
}
