// format.ts — pure display formatters for the alitellm-auth console.
//
// Ported verbatim (behavior + semantics) from src/ui/format.js. Every function
// is PURE: no DOM, no globals, no I/O — so the formatters are unit-testable
// without a renderer. The locked display strings come from 10-UI-SPEC
// §Typography:
//
//   formatCurrency(1249.5)                       -> "$1,249.50"
//   formatInt(1000000)                           -> "1,000,000"
//   abbreviate(2450000)                          -> "2.45M"
//   formatDate("2026-03-01T10:00:00+00:00")      -> "Mar 01, 2026"
//   maskKey("sk-abcd1234wxyz")                    -> "sk-a…wxyz"  (first4…last4)
//
// EVERY formatter returns the em-dash "—" (U+2014) for null/undefined (and any
// otherwise-unformattable input) and NEVER throws — null cells render as a
// neutral dash, not a crash.

const EM_DASH = '—'; // — (U+2014), the UI-SPEC null/empty placeholder
const ELLIPSIS = '…'; // … (U+2026), the mask ellipsis

// Coerce to a finite number, or null if the input is null/undefined/NaN.
function asNumber(n: number | string | null | undefined): number | null {
  if (n === null || n === undefined) return null;
  const num = typeof n === 'number' ? n : Number(n);
  return Number.isFinite(num) ? num : null;
}

const _currencyFmt = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const _intFmt = new Intl.NumberFormat('en-US', {
  maximumFractionDigits: 0,
});

// formatCurrency(n) -> "$1,249.50"; null/undefined/non-numeric -> "—".
export function formatCurrency(n: number | string | null | undefined): string {
  const num = asNumber(n);
  if (num === null) return EM_DASH;
  return _currencyFmt.format(num);
}

// formatInt(n) -> "1,000,000"; null/undefined/non-numeric -> "—".
export function formatInt(n: number | string | null | undefined): string {
  const num = asNumber(n);
  if (num === null) return EM_DASH;
  return _intFmt.format(num);
}

// abbreviate(n) -> "2.45M" / "1.5K" / "950"; up to 2 decimals, trailing zeros
// trimmed; null/undefined/non-numeric -> "—".
export function abbreviate(n: number | string | null | undefined): string {
  const num = asNumber(n);
  if (num === null) return EM_DASH;

  const abs = Math.abs(num);
  const units = [
    { value: 1e9, suffix: 'B' },
    { value: 1e6, suffix: 'M' },
    { value: 1e3, suffix: 'K' },
  ];

  for (const { value, suffix } of units) {
    if (abs >= value) {
      const scaled = num / value;
      // Up to 2 decimals, trailing zeros trimmed (2.45M, 1.5K, 2K).
      const trimmed = parseFloat(scaled.toFixed(2));
      return `${trimmed}${suffix}`;
    }
  }
  // Below 1000 — no suffix, integer form.
  return String(parseFloat(num.toFixed(2)));
}

// formatPricePerMillion(costPerToken) -> "$0.15" — the per-1M-token price from a
// LiteLLM per-token cost (×1e6), as USD currency. null/undefined/non-numeric ->
// "—". A genuine 0 cost renders "$0.00" (free), NOT a dash.
export function formatPricePerMillion(
  costPerToken: number | string | null | undefined,
): string {
  const num = asNumber(costPerToken);
  if (num === null) return EM_DASH;
  return _currencyFmt.format(num * 1e6);
}

// formatTokens(n) -> "128K" / "1M" (abbreviate), with null/undefined -> "—".
// A thin alias so the models table reads intent at the call site.
export function formatTokens(n: number | string | null | undefined): string {
  return abbreviate(n);
}

const _MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

// formatDate(iso) -> "Mar 01, 2026" (3-letter month, zero-padded day, UTC);
// null/undefined/unparseable -> "—". Never throws.
export function formatDate(iso: string | null | undefined): string {
  if (iso === null || iso === undefined) return EM_DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return EM_DASH;
  // Use UTC parts so the locked literal is stable regardless of host timezone.
  const month = _MONTHS[d.getUTCMonth()];
  const day = String(d.getUTCDate()).padStart(2, '0');
  const year = d.getUTCFullYear();
  return `${month} ${day}, ${year}`;
}

// maskKey(s) -> "first4…last4" — the value's first 4 and last 4 characters
// around an ellipsis (e.g. "sk-abcd1234wxyz" -> "sk-a…wxyz",
// "key-abc123" -> "key-…c123", a "088s…4fe6"-style hash id). null/undefined ->
// "—"; a value too short to mask meaningfully (<= 8 chars, where first4+last4
// would meet/overlap) is returned verbatim rather than fabricating an ellipsis.
// Never throws.
export function maskKey(s: string | null | undefined): string {
  if (s === null || s === undefined) return EM_DASH;
  const str = String(s);
  if (str.length <= 8) return str; // too short to mask — first4…last4 would overlap
  return `${str.slice(0, 4)}${ELLIPSIS}${str.slice(-4)}`;
}
