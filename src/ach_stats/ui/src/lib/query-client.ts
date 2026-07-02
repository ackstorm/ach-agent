// query-client.ts — the single configured TanStack Query client for the app.
//
// Minimal, sensible defaults: a 30s staleTime cuts redundant refetches on the
// short-lived dashboard, retry:1 tolerates one transient blip without hammering
// the backend, and refetchOnWindowFocus is off (a session dashboard does not
// need to refetch every time the tab regains focus). Hooks (Phases 3/4) consume
// this instance via QueryClientProvider.

import { QueryClient } from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
