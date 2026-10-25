import { useState } from "react";

import { ExecutionOutcome } from "@/components/execution-outcome";
import { PageHeader } from "@/components/page-shell";
import { TierBadge } from "@/components/tier-badge";
import { Button } from "@/components/ui/button";
import { cn } from "cn";

import { useApprovals, useDecide, type Approval, type ApprovalStatus } from "./approvals";

/**
 * Inbox of HIGH-risk actions the graph paused on.
 *
 * Two jobs, and the second is the one that needed the audit work. It lists
 * what is waiting and lets a human decide it — and for anything already
 * decided it says **who decided it and what that authorised**, read from the
 * `audit_log` rows that carry the approval's id. Before those columns were
 * written, answering that meant correlating by thread and timestamp, which
 * goes ambiguous as soon as one thread holds two approvals for the same tool.
 *
 * "Approved, never ran" is a state this shows rather than hides: an approval
 * can be decided and its execution still fail, and an inbox that implied
 * otherwise would be lying about the one thing it exists to record.
 */

const FILTERS: { label: string; status: ApprovalStatus | null }[] = [
  { label: "Waiting", status: "pending" },
  { label: "Approved", status: "approved" },
  { label: "Rejected", status: "rejected" },
  { label: "All", status: null },
];

export function ApprovalsPage() {
  // Waiting first: it is the actionable list, and an approval nobody has
  // decided is the one holding up a thread.
  const [status, setStatus] = useState<ApprovalStatus | null>("pending");
  const { data, isPending, isError, error } = useApprovals(status);

  return (
    <>
      <PageHeader
        title="Approvals"
        description="Actions waiting on a human before the agent may run them"
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl space-y-4 px-4 py-6">
          <div className="flex gap-1" role="tablist" aria-label="Filter approvals">
            {FILTERS.map((filter) => (
              <button
                key={filter.label}
                type="button"
                role="tab"
                aria-selected={status === filter.status}
                onClick={() => setStatus(filter.status)}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs font-medium",
                  status === filter.status
                    ? "bg-secondary text-secondary-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {filter.label}
              </button>
            ))}
          </div>

          {isPending ? <p className="text-sm text-muted-foreground">Loading…</p> : null}
          {isError ? (
            <p className="text-sm text-destructive" data-testid="approvals-error">
              Could not load approvals: {error.message}
            </p>
          ) : null}
          {data?.length === 0 ? (
            <p className="text-sm text-muted-foreground" data-testid="approvals-empty">
              Nothing here.
            </p>
          ) : null}

          {data?.map((approval) => <ApprovalCard key={approval.id} approval={approval} />)}
        </div>
      </div>
    </>
  );
}

function ApprovalCard({ approval }: { approval: Approval }) {
  const decide = useDecide();
  const pending = approval.status === "pending";

  return (
    <div className="space-y-3 rounded-md border p-3" data-testid="approval">
      <div className="flex items-center gap-2">
        <TierBadge tier={approval.tier} />
        <span className="text-xs font-medium">{approval.tool_name}</span>
        <span className="ml-auto text-xs text-muted-foreground">{approval.status}</span>
      </div>

      <p className="text-sm">{approval.summary ?? JSON.stringify(approval.args)}</p>

      {pending ? (
        <div className="flex gap-2">
          <Button
            size="sm"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ id: approval.id, decision: "approve" })}
          >
            Approve
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ id: approval.id, decision: "reject" })}
          >
            Reject
          </Button>
        </div>
      ) : (
        <Provenance approval={approval} />
      )}
    </div>
  );
}

/** Who decided it, and what that authorised. */
function Provenance({ approval }: { approval: Approval }) {
  return (
    <div className="space-y-1 border-l-2 pl-3 text-xs text-muted-foreground">
      <p data-testid="decided-by">
        {approval.status === "approved" ? "Approved" : "Rejected"} by{" "}
        <span className="font-medium text-foreground">{approval.decided_by ?? "unknown"}</span>
        {approval.decided_at ? ` · ${new Date(approval.decided_at).toLocaleString()}` : null}
      </p>
      <p>
        <ExecutionOutcome
          execution={approval.execution}
          status={approval.status}
          spent={approval.consumed_at !== null}
        />
      </p>
    </div>
  );
}
