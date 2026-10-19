/**
 * The approvals inbox's data.
 *
 * Separate from the page for the same reason `turn.ts` is: hooks and types
 * exported from a component file break React fast refresh.
 *
 * The shapes themselves live in `lib/api.ts`, beside `Health`: the chat page
 * reads an approval's execution too, and one feature importing another\'s
 * types is the wrong direction for that to travel in.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  apiGet,
  apiPost,
  type Approval,
  type ApprovalStatus,
  type Decision,
} from "@/lib/api";

export type { Approval, ApprovalStatus, Execution } from "@/lib/api";

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
      apiPost<Decision>(`/approvals/${id}`, {
        decision,
        decided_by: DECIDED_BY,
      }),
    // Every list is stale afterwards: the approval leaves `pending` and
    // arrives in `approved` or `rejected`, carrying an execution it did not
    // have a moment ago.
    onSettled: () => client.invalidateQueries({ queryKey: ["approvals"] }),
  });
}
