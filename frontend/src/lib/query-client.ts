import { QueryClient } from "@tanstack/react-query";

/**
 * One client for the app. Server state lives here rather than in component
 * state, per the project's code style rules.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Agent answers are expensive to produce, so a cached result is worth
      // more here than it would be in a CRUD app.
      staleTime: 30_000,
      // A failed request against a backend that is simply not running should
      // surface immediately rather than after three silent retries.
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
