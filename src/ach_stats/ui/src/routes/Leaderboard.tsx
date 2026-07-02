import { ErrorCard } from '@/components/layout/ErrorCard';
import { LoadingCard } from '@/components/layout/LoadingCard';
import { useLeaderboard } from '@/hooks/use-leaderboard';
import type { LeaderboardRow } from '@/lib/api-types';

function ScoreCell({ score }: { score: number | null }) {
  if (score === null) return <span className="text-muted-foreground italic">unrated</span>;
  return <span>{score.toFixed(1)}</span>;
}

export default function Leaderboard() {
  const { data, isPending, isError, refetch, isRefetching } = useLeaderboard(30);
  if (isPending) return <LoadingCard />;
  if (isError || !data) return <ErrorCard onRetry={() => refetch()} retrying={isRefetching} />;

  const { leaderboard, totals, range, recent } = data;
  const sinceBanner =
    totals.partial && range.coverage_start
      ? new Date(range.coverage_start).toLocaleDateString()
      : null;

  return (
    <div className="space-y-6 p-6">
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

      <div>
        <h2 className="mb-1 text-lg font-semibold">Leaderboard</h2>
        <p className="mb-3 text-xs uppercase tracking-wide text-muted-foreground">
          Ranked by {leaderboard.sorted_by}
        </p>
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase text-muted-foreground">
            <tr><th>Rank</th><th>Model</th><th>Provider</th><th>Score</th><th>Speed</th>
                <th>$/Mtok</th><th>Sessions</th><th>Tag</th></tr>
          </thead>
          <tbody>
            {leaderboard.rows.map((r: LeaderboardRow) => (
              <tr key={r.model} className="border-t border-border/50">
                <td>{r.rank}</td><td className="font-mono">{r.model}</td><td>{r.provider}</td>
                <td><ScoreCell score={r.score} /></td>
                <td>{r.speed_tok_s === null ? '—' : `${Math.round(r.speed_tok_s)} tok/s`}</td>
                <td>{r.cost_per_mtok === null ? '—' : `$${r.cost_per_mtok.toFixed(2)}`}</td>
                <td>{r.sessions}</td><td>{r.tag ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div>
        <h2 className="mb-3 text-lg font-semibold">Recent Sessions</h2>
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase text-muted-foreground">
            <tr><th>Task</th><th>Model</th><th>Tokens</th><th>Cost</th><th>Turns</th><th>Status</th></tr>
          </thead>
          <tbody>
            {recent.map((r, i) => (
              <tr key={i} className="border-t border-border/50">
                <td>{r.task}</td><td className="font-mono">{r.model}</td><td>{r.tokens}</td>
                <td>${r.cost.toFixed(2)}</td><td>{r.turns}</td>
                <td>{r.status}{r.retry ? ' · retry' : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
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
