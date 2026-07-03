import type { Contract } from '@/lib/api-types';

export function SessionsThisMonth({ data }: { data: Contract['sessions_this_month'] }) {
  if (data.rows.length === 0) {
    return <p className="text-sm text-muted-foreground">No sessions yet this month.</p>;
  }
  return (
    <div className="space-y-2">
      {data.partial && (
        <p className="text-xs text-muted-foreground">Partial — retention window started mid-month.</p>
      )}
      <ul className="divide-y divide-border/50 text-sm">
        {data.rows.map((r) => (
          <li key={r.model} className="flex items-center justify-between py-1.5">
            <span>{r.model}</span>
            <span className="text-muted-foreground">{r.count}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
