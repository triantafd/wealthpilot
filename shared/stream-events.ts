/**
 * The SSE event contract, shared by the backend and the frontend.
 *
 * Specified in `docs/ARCHITECTURE.md` section 6. The same shapes live as
 * Pydantic models in `backend/app/api/events.py`, and a contract test on each
 * side fails if they drift:
 *
 *   - `frontend/src/features/chat/stream-events.contract.test.ts` reads these
 *     declarations with the TypeScript compiler and compares them to the
 *     shape the backend exports.
 *   - `backend/tests/test_events_contract.py` regenerates that exported shape
 *     and fails if it is stale.
 *
 * Change an event here and you must change `events.py` too — working rule 5 in
 * CLAUDE.md. That rule was unenforceable until now: Phase 0 left `shared/`
 * holding only a `.gitkeep`, so there was nothing to keep in sync.
 *
 * Field names are camelCase because this is the wire format. The Python side
 * serialises through aliases to match.
 */

export type Tier = "READ" | "LOW" | "HIGH";

/** A retrieved passage, as the timeline shows it. Content is deliberately
 *  absent: the UI shows provenance and a score, and the full text would make
 *  every retrieval event the size of the corpus. */
export interface RetrievedChunkRef {
  id: number;
  documentId: string;
  page: number;
  score: number;
}

/** A quote the output guard has verified appears verbatim in a cited chunk. */
export interface Citation {
  documentId: string;
  page: number;
  quote: string;
}

/**
 * A mutation an agent wants to make, which a human must approve.
 *
 * Section 6 names this type without defining its fields; these mirror the
 * `approvals` table. `summary` exists because the approver is a person, and a
 * JSON blob of `args` is not something to base a decision on.
 */
export interface ProposedAction {
  tool: string;
  tier: Tier;
  args: Record<string, unknown>;
  summary: string;
}

/** Cost and latency for one request, sent once at the end. */
export interface Usage {
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  ms: number;
}

export type StreamEvent =
  | { type: "route"; route: string; confidence: number }
  | { type: "tool_start"; tool: string; tier: Tier; args: Record<string, unknown> }
  | { type: "tool_end"; tool: string; ms: number }
  | { type: "retrieval"; chunks: RetrievedChunkRef[] }
  /** A piece of the answer to append — or, with `replace`, the finished text
   *  to show instead of what was streamed. See `TokenEvent` in events.py. */
  | { type: "token"; text: string; replace: boolean }
  | { type: "approval_required"; approvalId: string; action: ProposedAction }
  | { type: "citations"; items: Citation[] }
  | { type: "guardrail"; flag: string }
  | { type: "done"; usage: Usage }
  | { type: "error"; message: string };

/** Narrowing helper, so a consumer can switch without casting. */
export function isStreamEvent(value: unknown): value is StreamEvent {
  return (
    typeof value === "object" &&
    value !== null &&
    "type" in value &&
    typeof (value as { type: unknown }).type === "string"
  );
}
