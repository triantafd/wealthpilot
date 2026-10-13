/**
 * One turn of the conversation, and how events fold into it.
 *
 * Separate from the component for two reasons: a pure reducer is testable
 * without a server or a DOM, and exporting a function from a component file
 * breaks React fast refresh.
 */

import type { Citation, ProposedAction, StreamEvent } from "@shared/stream-events";

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
  approval?: { approvalId: string; action: ProposedAction };
  decision?: "approve" | "reject";
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
