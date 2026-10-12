/**
 * One turn of the conversation, and how events fold into it.
 *
 * Separate from the component for two reasons: a pure reducer is testable
 * without a server or a DOM, and exporting a function from a component file
 * breaks React fast refresh.
 */

import type { Citation, ProposedAction, StreamEvent } from "@shared/stream-events";

export interface Turn {
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

export function newTurn(question: string): Turn {
  return { question, citations: [] };
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
      // Appended, not replaced. The answer is one event today and Phase 7 will
      // send it in pieces; appending is correct for both.
      return { ...turn, answer: (turn.answer ?? "") + event.text };
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
