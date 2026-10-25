/**
 * One turn of the conversation, and how events fold into it.
 *
 * Separate from the component for two reasons: a pure reducer is testable
 * without a server or a DOM, and exporting a function from a component file
 * breaks React fast refresh.
 */

import type { Execution, RestoredTurn } from "@/lib/api";
import type {
  ActionOutcome,
  Citation,
  ProposedAction,
  StreamEvent,
} from "@shared/stream-events";

export interface Turn {
  /**
   * Stable identity for this turn.
   *
   * Updates match on it rather than on "the last turn in the list". With one
   * turn on screen that distinction did not exist; with a conversation it
   * does, and `decide()` needs it too — approving must target the turn whose
   * approval it is, not whichever turn is newest.
   */
  id: string;
  question: string;
  route?: { route: string; confidence: number };
  retrievedCount?: number;
  answer?: string;
  citations: Citation[];
  /** What the action agent decided. `ask_account` and `refuse` both arrive as
   *  prose, so without this the screen cannot tell them apart. */
  outcome?: ActionOutcome;
  approval?: { approvalId: string; action: ProposedAction };
  decision?: "approve" | "reject";
  /** What the decision authorised, from the decide response. The turn's SSE
   *  stream ended at the interrupt, so this is the only channel it has. */
  executed?: Execution | null;
  /** Whether the approval was spent. An approval can be spent with no
   *  execution recorded against it; see `ExecutionOutcome`. */
  executionSpent?: boolean;
  error?: string;
  cost?: number;
  ms?: number;
}

let counter = 0;

export function newTurn(question: string): Turn {
  // A counter, not a timestamp: two turns submitted in the same millisecond
  // would collide, and React keys that collide drop a turn from the screen.
  counter += 1;
  return { id: `turn-${counter}`, question, citations: [] };
}

/**
 * A turn as it came back from the server, as the turn the page draws.
 *
 * The point of the mapping is that there is only one `Turn`: a restored
 * approval card is decided by the same `decide()` and rendered by the same
 * component as one that arrived over the stream, so the two cannot drift into
 * disagreeing about what an approval looks like.
 *
 * `index` supplies the id. Not `newTurn`'s counter: an id has to be stable
 * across renders because `decide` names the turn it is deciding, and it must
 * not collide with a turn created in this browser — hence the prefix.
 */
export function restore(wire: RestoredTurn, index: number): Turn {
  const approval = wire.approval;
  // Live means two things at once, and either alone is wrong: the graph is
  // still paused at the gate, and nobody has decided it. A card drawn from a
  // decided approval offers buttons the backend answers with a 409.
  const live = approval !== null && approval.status === "pending" && wire.awaiting.length > 0;
  const decision = decisionOf(approval);
  const cost = wire.usage?.cost_usd;

  return {
    id: `restored-${index}`,
    question: wire.question,
    // Both or neither: the route line prints the confidence, and a route
    // without one would render "confidence NaN".
    route:
      wire.route !== null && wire.route_confidence !== null
        ? { route: wire.route, confidence: wire.route_confidence }
        : undefined,
    retrievedCount: wire.retrieved_count,
    answer: wire.answer ?? undefined,
    citations: wire.citations,
    outcome: outcomeOf(wire.action_outcome),
    approval:
      live && approval
        ? {
            approvalId: approval.id,
            action: {
              tool: approval.tool_name,
              tier: approval.tier,
              args: approval.args,
              // The inbox and the stream both guarantee one; the column is
              // nullable, and an empty card is better than "null".
              summary: approval.summary ?? "",
            },
          }
        : undefined,
    decision,
    // Only alongside a decision, and `null` is a fact of its own — an approval
    // can be spent with nothing recorded against it. `ExecutionOutcome` is
    // where that is turned into words.
    executed: decision && approval ? approval.execution : undefined,
    executionSpent: decision && approval ? approval.consumed_at !== null : undefined,
    // A string on the wire; see `RestoredUsage`.
    cost: cost != null ? Number(cost) : undefined,
    ms: wire.usage?.latency_ms ?? undefined,
  };
}

/** What a human decided, in the words the page uses, or nothing.
 *
 *  `expired` is in the schema and nothing writes it, so it is deliberately
 *  neither: a card nobody can act on and no decision anybody took. Claiming
 *  "rejected" there would put words in an adviser's mouth. */
function decisionOf(approval: RestoredTurn["approval"]): "approve" | "reject" | undefined {
  switch (approval?.status) {
    case "approved":
      return "approve";
    case "rejected":
      return "reject";
    default:
      return undefined;
  }
}

/** The wire type is `string | null`, because the backend does not constrain
 *  it. Narrowed by matching rather than cast, so a value this page has no
 *  rendering for is dropped instead of reaching `OutcomeNotice` as a lie. */
function outcomeOf(outcome: string | null): ActionOutcome | undefined {
  switch (outcome) {
    case "propose":
    case "ask_account":
    case "refuse":
      return outcome;
    default:
      return undefined;
  }
}

/** Replace one turn in a conversation, leaving the others untouched. */
export function updateTurn(turns: Turn[], id: string, change: (turn: Turn) => Turn): Turn[] {
  return turns.map((turn) => (turn.id === id ? change(turn) : turn));
}

/**
 * Fold one event into the turn.
 *
 * This is where a missed event type becomes a blank screen, so every member of
 * the union is listed explicitly: `switch` with no `default` means adding an
 * event to the contract fails to compile until it is handled here.
 */
export function applyEvent(turn: Turn, event: StreamEvent): Turn {
  switch (event.type) {
    case "route":
      return { ...turn, route: { route: event.route, confidence: event.confidence } };
    case "retrieval":
      return { ...turn, retrievedCount: event.chunks.length };
    case "token":
      // Two shapes, one event: a delta to append while the answer is being
      // written, and the finished text at the end of the turn, which replaces
      // what was streamed because page references have been stripped out of
      // it. The backend decides which; see `TokenEvent` in app/api/events.py.
      return event.replace
        ? { ...turn, answer: event.text }
        : { ...turn, answer: (turn.answer ?? "") + event.text };
    case "citations":
      return { ...turn, citations: event.items };
    case "approval_required":
      return { ...turn, approval: { approvalId: event.approvalId, action: event.action } };
    case "action_outcome":
      return { ...turn, outcome: event.outcome };
    case "done":
      return { ...turn, cost: event.usage.costUsd, ms: event.usage.ms };
    case "error":
      return { ...turn, error: event.message };
    case "tool_start":
    case "tool_end":
    case "guardrail":
      // In the contract, not yet emitted — see app/api/stream.py. Listed rather
      // than defaulted, so when the backend starts sending them the missing
      // handling is a compile error rather than a silently ignored event.
      return turn;
  }
}
