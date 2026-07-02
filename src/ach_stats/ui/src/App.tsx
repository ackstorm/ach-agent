// App.tsx — ach-stats has exactly one page, so there is no router (unlike the
// alitellm-auth donor's session-gated hash router): just the shell wrapping the
// Leaderboard page.

import { AppShell } from '@/components/layout/AppShell';
import Leaderboard from '@/routes/Leaderboard';

export function App() {
  return (
    <AppShell>
      <Leaderboard />
    </AppShell>
  );
}
