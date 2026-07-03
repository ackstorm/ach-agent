// Shared chrome for the daily-usage area chart. Themed via CSS custom
// properties (recharts accepts `var(--x)` in stroke/fill strings) so it tracks
// the same tokens as the rest of the page — never a raw hex.

import type { TooltipProps } from 'recharts';

export const CHART_HEIGHT = 240;

export const CHART_EMPTY_COPY = 'No usage in this range';

export const AXIS_TICK = { fill: 'var(--text-tertiary)', fontSize: 11 } as const;
export const AXIS_LINE_STROKE = 'var(--border)';
export const SERIES_COLOR = 'var(--primary)';

export const TOOLTIP_CONTENT_STYLE = {
  background: 'var(--surface-elevated)',
  border: '1px solid var(--border)',
  borderRadius: 12,
  color: 'var(--text-primary)',
} as const;
export const TOOLTIP_LABEL_STYLE = { color: 'var(--text-secondary)' } as const;
export const TOOLTIP_ITEM_STYLE = { color: 'var(--text-primary)' } as const;

export function ChartEmpty() {
  return (
    <div
      className="flex items-center justify-center text-sm text-muted-foreground"
      style={{ minHeight: CHART_HEIGHT }}
    >
      {CHART_EMPTY_COPY}
    </div>
  );
}

export function makeTooltipFormatter(
  formatValue: (n: number) => string,
): NonNullable<TooltipProps<number, string>['formatter']> {
  return (value) => formatValue(typeof value === 'number' ? value : Number(value));
}
