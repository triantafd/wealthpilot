/**
 * Folding stream events into a turn.
 *
 * A pure reducer, so these need no server and no DOM. This is where a missed
 * event type becomes a blank screen, so every event the backend sends has a
 * case here.
 */

import { describe, expect, it } from "vitest";

import type { StreamEvent } from "@shared/stream-events";

import { applyEvent, newTurn } from "./turn";

const start = () => newTurn("Is there a floor on what we charge?");

describe("applyEvent", () => {
  it("records the route and its confidence", () => {
    const turn = applyEvent(start(), { type: "route", route: "research", confidence: 0.91 });

    expect(turn.route).toEqual({ route: "research", confidence: 0.91 });
  });

  it("counts retrieved passages without keeping their text", () => {
    const turn = applyEvent(start(), {
      type: "retrieval",
      chunks: [
        { id: 1, documentId: "fee-schedule", page: 2, score: 0.9 },
        { id: 2, documentId: "compliance-faq", page: 1, score: 0.8 },
      ],
    });

    expect(turn.retrievedCount).toBe(2);
  });

  it("appends tokens rather than replacing them", () => {
    // The answer is one event today and Phase 7 will send it in pieces;
    // appending is correct for both, and replacing would silently truncate
    // the answer the day the backend changes.
    let turn = applyEvent(start(), { type: "token", text: "The minimum is " });
    turn = applyEvent(turn, { type: "token", text: "£1,500." });

    expect(turn.answer).toBe("The minimum is £1,500.");
  });

  it("keeps the citations the backend verified", () => {
    const turn = applyEvent(start(), {
      type: "citations",
      items: [{ documentId: "fee-schedule", page: 1, quote: "a minimum annual fee of £1,500" }],
    });

    expect(turn.citations).toHaveLength(1);
    expect(turn.citations[0]?.documentId).toBe("fee-schedule");
  });

  it("holds the pending approval so the page can offer a decision", () => {
    const turn = applyEvent(start(), {
      type: "approval_required",
      approvalId: "a-1",
      action: {
        tool: "propose_rebalance",
        tier: "HIGH",
        args: { account_id: "AC-0042" },
        summary: "Raise a rebalance task for AC-0042.",
      },
    });

    expect(turn.approval?.approvalId).toBe("a-1");
    expect(turn.approval?.action.tier).toBe("HIGH");
  });

  it("records cost and latency from done", () => {
    const turn = applyEvent(start(), {
      type: "done",
      usage: { inputTokens: 2612, outputTokens: 48, costUsd: 0.000421, ms: 2814 },
    });

    expect(turn.cost).toBe(0.000421);
    expect(turn.ms).toBe(2814);
  });

  it("surfaces an error event", () => {
    const turn = applyEvent(start(), { type: "error", message: "provider exploded" });

    expect(turn.error).toBe("provider exploded");
  });

  it("ignores events the backend does not send yet without losing state", () => {
    // tool_start, tool_end and guardrail are in the contract and unemitted.
    // They must not blank the turn if the backend starts sending them before
    // this page handles them.
    const withAnswer = applyEvent(start(), { type: "token", text: "£1,500." });
    const events: StreamEvent[] = [
      { type: "tool_start", tool: "propose_rebalance", tier: "HIGH", args: {} },
      { type: "tool_end", tool: "propose_rebalance", ms: 12 },
      { type: "guardrail", flag: "injection_suspected" },
    ];

    const after = events.reduce(applyEvent, withAnswer);

    expect(after.answer).toBe("£1,500.");
  });

  it("handles a whole research turn in order", () => {
    const events: StreamEvent[] = [
      { type: "route", route: "research", confidence: 0.9 },
      { type: "retrieval", chunks: [{ id: 1, documentId: "fee-schedule", page: 1, score: 0.9 }] },
      { type: "token", text: "The minimum annual advisory fee is £1,500." },
      { type: "citations", items: [{ documentId: "fee-schedule", page: 1, quote: "£1,500" }] },
      { type: "done", usage: { inputTokens: 10, outputTokens: 2, costUsd: 0.000001, ms: 100 } },
    ];

    const turn = events.reduce(applyEvent, start());

    expect(turn.route?.route).toBe("research");
    expect(turn.answer).toContain("£1,500");
    expect(turn.citations).toHaveLength(1);
    expect(turn.error).toBeUndefined();
  });
});
