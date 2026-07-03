// Daily spend/sessions area chart over the `series` field of the leaderboard
// contract (computed server-side, previously never rendered).

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { Button } from '@/components/ui/button';
import type { SeriesPoint } from '@/lib/api-types';
import { formatCurrency, formatDate, formatInt } from '@/lib/format';

import {
  AXIS_LINE_STROKE,
  AXIS_TICK,
  CHART_HEIGHT,
  ChartEmpty,
  makeTooltipFormatter,
  SERIES_COLOR,
  TOOLTIP_CONTENT_STYLE,
  TOOLTIP_ITEM_STYLE,
  TOOLTIP_LABEL_STYLE,
} from './chart-common';

export type DailyMetric = 'spend' | 'sessions';

const FORMATTERS: Record<DailyMetric, (n: number) => string> = {
  spend: formatCurrency,
  sessions: formatInt,
};

export function DailyMetricToggle({
  metric,
  onChange,
}: {
  metric: DailyMetric;
  onChange: (m: DailyMetric) => void;
}) {
  return (
    <div className="flex gap-1">
      <Button
        type="button"
        size="xs"
        variant={metric === 'spend' ? 'default' : 'outline'}
        onClick={() => onChange('spend')}
      >
        Spend
      </Button>
      <Button
        type="button"
        size="xs"
        variant={metric === 'sessions' ? 'default' : 'outline'}
        onClick={() => onChange('sessions')}
      >
        Sessions
      </Button>
    </div>
  );
}

export function DailyChart({ series, metric }: { series: SeriesPoint[]; metric: DailyMetric }) {
  if (series.length === 0) return <ChartEmpty />;
  const format = FORMATTERS[metric];

  return (
    <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
      <AreaChart data={series}>
        <defs>
          <linearGradient id="dailyFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={SERIES_COLOR} stopOpacity={0.35} />
            <stop offset="100%" stopColor={SERIES_COLOR} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={AXIS_LINE_STROKE} strokeDasharray="3 3" vertical={false} />
        <XAxis
          dataKey="date"
          tickFormatter={formatDate}
          tick={AXIS_TICK}
          axisLine={{ stroke: AXIS_LINE_STROKE }}
          tickLine={{ stroke: AXIS_LINE_STROKE }}
          minTickGap={24}
          interval="preserveStartEnd"
        />
        <YAxis
          width={64}
          tickFormatter={format}
          tick={AXIS_TICK}
          axisLine={{ stroke: AXIS_LINE_STROKE }}
          tickLine={{ stroke: AXIS_LINE_STROKE }}
        />
        <Tooltip
          contentStyle={TOOLTIP_CONTENT_STYLE}
          labelStyle={TOOLTIP_LABEL_STYLE}
          itemStyle={TOOLTIP_ITEM_STYLE}
          labelFormatter={(label) => formatDate(String(label))}
          formatter={makeTooltipFormatter(format)}
        />
        <Area
          type="monotone"
          dataKey={metric}
          stroke={SERIES_COLOR}
          strokeWidth={2}
          fill="url(#dailyFill)"
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
