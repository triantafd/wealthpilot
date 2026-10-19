import type { ApprovalStatus, Execution } from "@/lib/api";

/**
 * What a decided approval actually did, in a sentence.
 *
 * Shared by the approvals inbox and the chat timeline because the hard part
 * is not the rendering, it is being exact about four different situations
 * that a careless version flattens into "approved":
 *
 *  * it ran, and here is what it raised;
 *  * it ran and failed, and here is why;
 *  * nothing ran, because the action was rejected;
 *  * nothing ran under an approval that was *approved* — the execution failed
 *    before the tool did anything, or the thread was no longer waiting;
 *  * something ran and no row can name it, because the approval was spent
 *    before `audit_log.approval_id` was ever written. Saying "nothing ran"
 *    there is a false statement about a task that exists.
 *
 * Two copies of that reasoning would have drifted within a week.
 */
export function ExecutionOutcome({
  execution,
  status,
  spent,
}: {
  execution: Execution | null;
  status: ApprovalStatus;
  spent: boolean;
}) {
  if (execution === null) {
    return (
      <span data-testid="outcome">
        {status === "rejected"
          ? "Nothing ran."
          : spent
            ? "Spent, but no execution was recorded against it."
            : "Approved, but nothing ran under it."}
      </span>
    );
  }

  const taskId = execution.result?.task_id;
  return (
    <span data-testid="outcome">
      {execution.outcome === "ok"
        ? `Ran${taskId ? ` · raised task ${String(taskId)}` : ""}`
        : `Did not run · ${execution.error ?? execution.outcome}`}
      {execution.attempts > 1 ? ` · offered ${execution.attempts} times` : null}
    </span>
  );
}
