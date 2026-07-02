// LoadingCard.tsx — the loading-state shell card, adapted from alitellm-auth's
// donor component (src/ui/app.js LoadingCard equivalent) for ach-stats.

import { Card } from '@/components/ui/card';

export function LoadingCard() {
  return (
    <div
      data-state="loading"
      role="status"
      aria-live="polite"
      className="flex flex-1 items-center justify-center p-8 min-h-screen"
    >
      <Card className="w-full max-w-[620px] gap-0 py-0">
        <div className="border-b border-border px-8 pt-8 pb-6">
          <div className="mb-4 flex items-center gap-2.5">
            <span
              aria-hidden="true"
              className="size-2 animate-pulse rounded-full bg-primary shadow-[0_0_8px_var(--primary)]"
            />
            <span className="font-mono text-xs font-semibold tracking-widest text-primary">
              LOADING
            </span>
          </div>
          <h1 className="text-2xl font-semibold leading-tight text-text-primary">
            Loading stats...
          </h1>
          <p className="mt-1.5 text-sm text-text-secondary">
            Fetching the leaderboard, please wait.
          </p>
        </div>
      </Card>
    </div>
  );
}
