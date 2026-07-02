export interface LeaderboardRow {
  rank: number; model: string; provider: string; tag: string | null;
  score: number | null; speed_tok_s: number | null; cost_per_mtok: number | null;
  spend: number; sessions: number;
}
export interface Totals {
  sessions: number; tokens: number; spend: number;
  avg_cost_per_session: number | null; aborted: number; partial: boolean;
}
export interface RecentRow {
  ts: number; task: string; model: string; tokens: number; cost: number;
  turns: number; status: string; retry: boolean;
}
export interface SeriesPoint { date: string; spend: number; sessions: number; tokens: number; partial: boolean; }
export interface Contract {
  range: { start: number; end: number; days: number; coverage_start: number | null; tz: string };
  totals: Totals;
  leaderboard: { sorted_by: 'spend' | 'score'; rows: LeaderboardRow[] };
  cost_per_session: { model: string; avg: number | null }[];
  sessions_this_month: { rows: { model: string; count: number }[]; partial: boolean };
  series: SeriesPoint[];
  recent: RecentRow[];
}
