/**
 * A thread's own history, for a page that arrived with an id in its URL.
 *
 * Separate from the component for the same reason `turn.ts` is: a hook
 * exported from a component file breaks React fast refresh. The mapping into
 * the page's `Turn` is in `turn.ts` beside `applyEvent`, where it can be
 * tested without a query client.
 */

import { useQuery } from "@tanstack/react-query";

import { apiGet, type ThreadTurns } from "@/lib/api";

/**
 * The turns of a thread. `null` asks for nothing, and issues no request.
 *
 * Only ever called with an id the page *arrived* with. A thread this page just
 * created has no history to restore — its first turn is still streaming — and
 * fetching it would race the stream for the same turn.
 */
export function useThreadTurns(threadId: string | null) {
  return useQuery({
    queryKey: ["thread", threadId, "turns"],
    queryFn: ({ signal }) => {
      if (threadId === null) {
        // Unreachable: `enabled` is false. Here so the id needs no `!`.
        throw new Error("no thread to restore");
      }
      return apiGet<ThreadTurns>(`/threads/${threadId}/turns`, signal);
    },
    enabled: threadId !== null,
    // A restored transcript is what already happened, and the page is the
    // thing that moves it on from there. Refetching would replay turns the
    // adviser is looking at, so it never goes stale on its own.
    staleTime: Infinity,
    // A thread that is not there is not a transient failure, and the page has
    // a sentence to say about it rather than three silent attempts first.
    retry: false,
  });
}
