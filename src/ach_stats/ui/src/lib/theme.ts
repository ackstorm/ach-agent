// theme.ts — light/dark theme resolution + application helpers.
//
// The console ships dark-first (index.html sets class="dark" and an inline
// pre-paint script applies the stored/system theme before React mounts, so there
// is no flash). These helpers are the single source of truth for: the storage
// key, reading a stored choice, the system fallback, and mutating the <html>
// class. Kept side-effect-light and guarded (no throw if storage/matchMedia is
// unavailable) so they are safe to call at module load and in tests.

export type Theme = 'dark' | 'light' | 'pastel' | 'red';

export const THEME_STORAGE_KEY = 'ach-stats-theme';

/** The toggle cycle order: dark → light → pastel → red → (dark). */
export const THEME_ORDER: readonly Theme[] = ['dark', 'light', 'pastel', 'red'];

/** The class names this app ever puts on <html> for theming. */
const THEME_CLASSES = ['dark', 'light', 'pastel', 'red'] as const;

/** The next theme in the cycle (wraps around). */
export function nextTheme(theme: Theme): Theme {
  const i = THEME_ORDER.indexOf(theme);
  return THEME_ORDER[(i + 1) % THEME_ORDER.length];
}

/** The OS preference, defaulting to dark when matchMedia is unavailable.
 *  Note: pastel is an explicit choice only — the OS never resolves to it. */
export function getSystemTheme(): Theme {
  if (typeof window === 'undefined' || !window.matchMedia) return 'dark';
  return window.matchMedia('(prefers-color-scheme: light)').matches
    ? 'light'
    : 'dark';
}

/** The persisted choice, or null when unset/unreadable. */
export function readStoredTheme(): Theme | null {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY);
    return v === 'dark' || v === 'light' || v === 'pastel' || v === 'red'
      ? v
      : null;
  } catch {
    return null;
  }
}

/** Stored choice wins; otherwise fall back to the OS preference. */
export function resolveInitialTheme(): Theme {
  return readStoredTheme() ?? getSystemTheme();
}

/** Mutate <html> so exactly one of `dark`/`light`/`pastel` is present. */
export function applyThemeClass(theme: Theme): void {
  if (typeof document === 'undefined') return;
  const el = document.documentElement;
  el.classList.remove(...THEME_CLASSES);
  el.classList.add(theme);
}

/** Persist the explicit choice (no-op if storage is unavailable). */
export function persistTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    /* ignore — a denied/unavailable storage just means no persistence */
  }
}
