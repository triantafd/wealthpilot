import { NotWiredYet, PageHeader } from "@/components/page-shell";

/** Inbox of HIGH-risk actions the graph paused on. Phase 5. */
export function ApprovalsPage() {
  return (
    <>
      <PageHeader
        title="Approvals"
        description="Actions waiting on a human before the agent may run them"
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <NotWiredYet
          phase="Phase 5 · Actions and human-in-the-loop"
          will={[
            "Every pending action, with the context the agent gathered to justify it",
            "Approve or reject with a note, resuming the paused graph",
            "Decisions that survive a server restart, via the Postgres checkpointer",
          ]}
          needs="the interrupt() approval gate and the /approvals endpoints"
        />
      </div>
    </>
  );
}
