// Days-window presets. The backend only supports a rolling `days` window
// (GET /api/leaderboard?days=N, 1<=N<=62) — no arbitrary start/end — so this
// is a plain preset row, not a calendar/custom-range picker.

import { Button } from '@/components/ui/button';

const PRESETS = [7, 14, 30, 60] as const;

export function RangePresets({
  days,
  onChange,
}: {
  days: number;
  onChange: (days: number) => void;
}) {
  return (
    <div className="flex gap-1">
      {PRESETS.map((d) => (
        <Button
          key={d}
          type="button"
          size="sm"
          variant={days === d ? 'default' : 'outline'}
          onClick={() => onChange(d)}
        >
          {d}d
        </Button>
      ))}
    </div>
  );
}
