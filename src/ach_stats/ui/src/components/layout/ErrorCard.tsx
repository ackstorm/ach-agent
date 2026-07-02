// ErrorCard.tsx — the error-state shell card, adapted from alitellm-auth's
// donor component for ach-stats (no session retry — just a failed contract fetch).

import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';

export interface ErrorCardProps {
  onRetry: () => void;
  retrying?: boolean;
}

export function ErrorCard({ onRetry, retrying = false }: ErrorCardProps) {
  return (
    <div
      data-state="error"
      role="alert"
      className="flex flex-1 items-center justify-center p-8 min-h-screen"
    >
      <Card className="w-full max-w-[620px] gap-0 py-0">
        <div className="border-b border-border px-8 pt-8 pb-6">
          <div className="mb-4 flex items-center gap-2.5">
            <span
              aria-hidden="true"
              className="size-2 rounded-full bg-destructive shadow-[0_0_8px_var(--destructive)]"
            />
            <span className="font-mono text-xs font-semibold tracking-widest text-destructive">
              ERROR
            </span>
          </div>
          <h1 className="text-2xl font-semibold leading-tight text-text-primary">
            Failed to load stats
          </h1>
          <p className="mt-1.5 text-sm text-text-secondary">
            Unable to reach ach-stats. Try refreshing the page.
          </p>
        </div>
        <div className="px-8 py-7">
          <Button
            type="button"
            variant="destructive"
            onClick={onRetry}
            disabled={retrying}
            className="font-mono lowercase tracking-wide"
          >
            retry
          </Button>
        </div>
      </Card>
    </div>
  );
}
