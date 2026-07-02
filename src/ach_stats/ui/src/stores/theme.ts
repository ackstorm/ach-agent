// theme.ts — Zustand store for the light/dark theme choice.
//
// Idiom matches the other stores (plain `create<T>((set, get) => ...)`). The
// initial value is resolved from storage/system (the same logic the index.html
// pre-paint script uses, so the store value matches the class already on <html>
// at first render — no flash, no mismatch). setTheme is the single mutator: it
// applies the <html> class AND persists, so the toggle button is a one-liner.

import { create } from 'zustand';

import {
  type Theme,
  applyThemeClass,
  nextTheme,
  persistTheme,
  resolveInitialTheme,
} from '@/lib/theme';

export interface ThemeState {
  theme: Theme;
  /** Set + apply + persist an explicit theme. */
  setTheme: (theme: Theme) => void;
  /** Cycle through the themes: dark → light → pastel → red → dark. */
  toggle: () => void;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: resolveInitialTheme(),
  setTheme: (theme) => {
    applyThemeClass(theme);
    persistTheme(theme);
    set({ theme });
  },
  toggle: () => get().setTheme(nextTheme(get().theme)),
}));
