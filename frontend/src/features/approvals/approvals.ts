/**
 * The approvals inbox's data.
 *
 * Separate from the page for the same reason `turn.ts` is: hooks and types
 * exported from a component file break React fast refresh.
 *
 * These shapes are hand-written rather than shared, unlike the SSE contract in
 * `shared/stream-events.ts`. The REST models are snake_case on the wire — only
 * the stream serialises through camelCase aliases — so the names here are the
 * backend's own, and a drift would show up as a missing field rather than as
 * a silent undefined.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPost } from "@/lib/api";

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

/** What the approval authorised, once it was spent. Absent until something
 *  runs under it — a pending approval, a rejected one, and an approved one
 *  whose execution never happened all have none. */
export interface Execution {
  outcome: "ok" | "error" | "denied";
  at: string;
  duration_ms: number | null;
  result: Record<string, unknown> | null;
  error: string | null;
  attempts: number;
}

export interface Approval {
  id: string;
  thread_id: string;
  tool_name: string;
  tier: "READ" | "LOW" | "HIGH";
  args: Record<string, unknown>;
  summary: string | null;
  status: ApprovalStatus;
  requested_by: string | null;
  decided_by: string | null;
  decision_note: string | null;
  created_at: string;
  decided_at: string | null;
  consumed_at: string | null;
  execution: Execution | null;
}

/** The inbox, filtered. `null` asks for every status. */
export function useApprovals(status: ApprovalStatus | null) {
  return useQuery({
    queryKey: ["approvals", status],
    queryFn: ({ signal }) =>
      apiGet<Approval[]>(`/approvals${status ? `?status=${status}` : ""}`, signal),
  });
}

/** Until there is authentication, the decider is a placeholder — the same one
 *  the chat page sends, and the same Phase 9 gap. The backend says so too. */
const DECIDED_BY = "adviser-7";

export function useDecide() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      apiPost<{ resumed: boolean }>(`/approvals/${id}`, {
        decision,
        decided_by: DECIDED_BY,
      }),
    // Every list is stale afterwards: the approval leaves `pending` and
    // arrives in `approved` or `rejected`, carrying an execution it did not
    // have a moment ago.
    onSettled: () => client.invalidateQueries({ queryKey: ["approvals"] }),
  });
}
