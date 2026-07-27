// ThemeToggle.tsx — the dark / light / pastel theme cycler (topbar + login).
//
// One button that CYCLES the theme: dark → light → pastel → red → dark.
// Following the common affordance, it shows the icon of the theme it will switch
// TO (a sun in dark mode → "go light"; a palette in light mode → "go pastel"; a
// flame in pastel mode → "go red"; a moon in red mode → "go dark"). The owner
// applies the <html> class and persists, so this is purely presentational.

import { Flame, Moon, Palette, Sun } from 'lucide-react';

import { type Theme, nextTheme } from '@/lib/theme';

// Icon + copy for the theme each click moves TO, keyed by the CURRENT theme.
const NEXT_ICON = { dark: Sun, light: Palette, pastel: Flame, red: Moon } as const;
const NEXT_LABEL = {
  dark: 'Switch to light theme',
  light: 'Switch to pastel theme',
  pastel: 'Switch to red theme',
  red: 'Switch to dark theme',
} as const;
const NEXT_TITLE = {
  dark: 'Light mode',
  light: 'Pastel mode',
  pastel: 'Red mode',
  red: 'Dark mode',
} as const;

export interface ThemeToggleProps {
  theme: Theme;
  onToggle: () => void;
}

export function ThemeToggle({ theme, onToggle }: ThemeToggleProps) {
  const Icon = NEXT_ICON[theme];

  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={NEXT_LABEL[theme]}
      title={`${NEXT_TITLE[theme]} (next: ${nextTheme(theme)})`}
      data-theme={theme}
      className="inline-flex size-8 cursor-pointer items-center justify-center rounded-md border border-border text-text-secondary transition-colors hover:border-primary hover:text-primary"
    >
      <Icon className="size-[15px]" aria-hidden="true" />
    </button>
  );
}
