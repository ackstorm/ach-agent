import { useState } from 'react';

import { ErrorCard } from '@/components/layout/ErrorCard';
import { LoadingCard } from '@/components/layout/LoadingCard';
import { DailyChart, DailyMetricToggle, type DailyMetric } from '@/components/stats/DailyChart';
import { RangePresets } from '@/components/stats/RangePresets';
import { SessionsThisMonth } from '@/components/stats/SessionsThisMonth';
import { useLeaderboard } from '@/hooks/use-leaderboard';
import type { LeaderboardRow } from '@/lib/api-types';

function ScoreCell({ score }: { score: number | null }) {
  if (score === null) return <span className="text-muted-foreground italic">unrated</span>;
  return <span>{score.toFixed(1)}</span>;
}

function Panel({
  label,
  action,
  children,
}: {
  label: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border/50 bg-card p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-xs uppercase tracking-wide text-muted-foreground">{label}</h2>
        {action}
      </div>
      {children}
    </div>
  );
}

export default function Leaderboard() {
  const [days, setDays] = useState(7);
  const [dailyMetric, setDailyMetric] = useState<DailyMetric>('spend');
  const { data, isPending, isError, refetch, isRefetching } = useLeaderboard(days);
  if (isPending) return <LoadingCard />;
  if (isError || !data) return <ErrorCard onRetry={() => refetch()} retrying={isRefetching} />;

  const { leaderboard, totals, range, recent, series, sessions_this_month } = data;
  const sinceBanner =
    totals.partial && range.coverage_start
      ? new Date(range.coverage_start).toLocaleDateString()
      : null;

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-end">
        <RangePresets days={days} onChange={setDays} />
      </div>

      {sinceBanner && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-2 text-sm">
          Showing data since {sinceBanner} (older data past retention).
        </div>
      )}

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Kpi label="Total Sessions" value={String(totals.sessions)} />
        <Kpi label="Total Spend" value={`$${totals.spend.toFixed(2)}`} />
        <Kpi label="Aborted" value={String(totals.aborted)} />
        <Kpi label="Avg $/Session"
             value={totals.avg_cost_per_session === null ? '—' : `$${totals.avg_cost_per_session.toFixed(3)}`} />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <Panel
            label="Daily Usage"
            action={<DailyMetricToggle metric={dailyMetric} onChange={setDailyMetric} />}
          >
            <DailyChart series={series} metric={dailyMetric} />
          </Panel>
        </div>
        <Panel label="Sessions This Month">
          <SessionsThisMonth data={sessions_this_month} />
        </Panel>
      </div>

      <div>
        <h2 className="mb-1 text-lg font-semibold">Leaderboard</h2>
        <p className="mb-3 text-xs uppercase tracking-wide text-muted-foreground">
          Ranked by {leaderboard.sorted_by}
        </p>
        <div className="overflow-x-auto rounded-lg border border-border/50">
          <table className="w-full border-collapse text-sm">
            <thead className="text-left text-xs uppercase text-muted-foreground">
              <tr className="border-b border-border/50">
                <th className="px-3 py-2">Rank</th><th className="px-3 py-2">Model</th>
                <th className="px-3 py-2">Provider</th><th className="px-3 py-2">Score</th>
                <th className="px-3 py-2">Speed</th><th className="px-3 py-2">$/Mtok</th>
                <th className="px-3 py-2">Sessions</th><th className="px-3 py-2">Tag</th>
              </tr>
            </thead>
            <tbody>
              {leaderboard.rows.map((r: LeaderboardRow) => (
                <tr key={r.model} className="border-t border-border/50">
                  <td className="px-3 py-2">{r.rank}</td>
                  <td className="px-3 py-2">{r.model}</td>
                  <td className="px-3 py-2">{r.provider}</td>
                  <td className="px-3 py-2"><ScoreCell score={r.score} /></td>
                  <td className="px-3 py-2">
                    {r.speed_tok_s === null ? '—' : `${Math.round(r.speed_tok_s)} tok/s`}
                  </td>
                  <td className="px-3 py-2">
                    {r.cost_per_mtok === null ? '—' : `$${r.cost_per_mtok.toFixed(2)}`}
                  </td>
                  <td className="px-3 py-2">{r.sessions}</td>
                  <td className="px-3 py-2">{r.tag ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div>
        <h2 className="mb-3 text-lg font-semibold">Recent Sessions</h2>
        <div className="overflow-x-auto rounded-lg border border-border/50">
          <table className="w-full border-collapse text-sm">
            <thead className="text-left text-xs uppercase text-muted-foreground">
              <tr className="border-b border-border/50">
                <th className="px-3 py-2">Task</th><th className="px-3 py-2">Model</th>
                <th className="px-3 py-2">Tokens</th><th className="px-3 py-2">Cost</th>
                <th className="px-3 py-2">Turns</th><th className="px-3 py-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((r, i) => (
                <tr key={i} className="border-t border-border/50">
                  <td className="max-w-xs truncate px-3 py-2" title={r.task}>{r.task}</td>
                  <td className="px-3 py-2">{r.model}</td>
                  <td className="px-3 py-2">{r.tokens}</td>
                  <td className="px-3 py-2">${r.cost.toFixed(2)}</td>
                  <td className="px-3 py-2">{r.turns}</td>
                  <td className="px-3 py-2">{r.status}{r.retry ? ' · retry' : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border/50 bg-card p-4">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
    </div>
  );
}
