/**
 * The conversations, for the sidebar's list.
 *
 * Separate from the component for the same reason `turn.ts` and `history.ts`
 * are: a hook exported from a component file breaks React fast refresh.
 */

import { useQuery } from "@tanstack/react-query";

import { apiGet, type ThreadList } from "@/lib/api";

export type { ThreadSummary } from "@/lib/api";

/** The key the chat page invalidates when a turn finishes. */
export const THREADS_KEY = ["threads"] as const;

/**
 * Every conversation that has had a turn, newest first.
 *
 * No `staleTime`: the list goes out of date for a reason this app can see —
 * a turn was sent — and the chat page invalidates it when one ends. Leaving it
 * fresh forever would also miss a conversation started in another tab, which
 * the default refetch-on-focus picks up for nothing.
 */
export function useThreads() {
  return useQuery({
    queryKey: THREADS_KEY,
    queryFn: ({ signal }) => apiGet<ThreadList>("/threads", signal),
    select: (data) => data.threads,
  });
}
