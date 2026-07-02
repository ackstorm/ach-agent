// AppShell.tsx — the ach-stats page chrome: a topbar (brand + theme toggle),
// a centered content region, and the shared footer.
//
// Unlike the alitellm-auth donor's AppShell, there is no session/auth, no nav
// (one page), no user menu, and no modals — ach-stats is a read-only dashboard.

import type { ReactNode } from 'react';

import { SiteFooter } from './SiteFooter';
import { ThemeToggle } from './ThemeToggle';

export interface AppShellProps {
  children: ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  return (
    <div className="flex min-h-screen flex-col bg-background">
      <header className="sticky top-0 z-40 flex h-14 shrink-0 items-center gap-4 border-b border-border bg-surface px-4 md:px-6">
        <span className="inline-flex items-center gap-2 font-sans font-semibold text-text-primary">
          <svg
            viewBox="0 0 24 24"
            aria-hidden="true"
            className="stroke-primary size-[18px] fill-none"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M12 2 4 6v6c0 4.5 3.4 7.3 8 10 4.6-2.7 8-5.5 8-10V6z" />
            <path d="m9 12 2 2 4-4" />
          </svg>
          ach-stats
        </span>
        <div className="ml-auto flex items-center gap-2">
          <ThemeToggle />
        </div>
      </header>

      <div className="mx-auto flex w-full max-w-[1200px] flex-1 flex-col px-6 py-8">
        <main className="min-w-0 flex-1 animate-content-in">{children}</main>
      </div>

      <SiteFooter />
    </div>
  );
}
