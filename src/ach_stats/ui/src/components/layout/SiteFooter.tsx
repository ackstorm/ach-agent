// SiteFooter.tsx — the shared site footer, adapted from alitellm-auth's donor
// component. ach-stats has no config-fetching backend, so `brand` is a fixed
// string and there are no privacy/terms links (real-links-only: nothing to omit
// when there's nothing to link).

import type { ReactNode } from 'react';

export interface SiteFooterProps {
  /** Optional node rendered on the footer's right side (e.g. a status indicator). */
  rightSlot?: ReactNode;
}

export function SiteFooter({ rightSlot }: SiteFooterProps) {
  const year = new Date().getFullYear();

  return (
    <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-border bg-surface px-6 py-4 font-sans text-sm text-text-secondary">
      <span className="text-text-secondary">© {year} ach-stats.</span>
      {rightSlot ? <span className="ml-auto inline-flex items-center gap-4">{rightSlot}</span> : null}
    </footer>
  );
}
