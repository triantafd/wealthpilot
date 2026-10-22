/**
 * Folding stream events into a turn.
 *
 * A pure reducer, so these need no server and no DOM. This is where a missed
 * event type becomes a blank screen, so every event the backend sends has a
 * case here.
 */

import { describe, expect, it } from "vitest";

import type { Approval, RestoredTurn } from "@/lib/api";
import type { StreamEvent } from "@shared/stream-events";

import { applyEvent, newTurn, restore } from "./turn";

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
    // How the answer arrives while it is being written. Replacing here would
    // show only the last few words of every answer.
    let turn = applyEvent(start(), {
      type: "token",
      text: "The minimum is ",
      replace: false,
    });
    turn = applyEvent(turn, { type: "token", text: "£1,500.", replace: false });

    expect(turn.answer).toBe("The minimum is £1,500.");
  });

  it("shows the finished text instead of the streamed prose", () => {
    // The last token event of a turn carries the answer after the backend has
    // stripped its page references, so it replaces rather than appends. Doing
    // this by appending would print the answer twice.
    let turn = applyEvent(start(), {
      type: "token",
      text: "The minimum is £1,500 (page 1).",
      replace: false,
    });
    turn = applyEvent(turn, {
      type: "token",
      text: "The minimum is £1,500.",
      replace: true,
    });

    expect(turn.answer).toBe("The minimum is £1,500.");
  });

  it("keeps which outcome the action agent reached", () => {
    // Asking and refusing both arrive as prose; this is the only thing that
    // tells them apart.
    const turn = applyEvent(start(), { type: "action_outcome", outcome: "ask_account" });

    expect(turn.outcome).toBe("ask_account");
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
    const withAnswer = applyEvent(start(), {
      type: "token",
      text: "£1,500.",
      replace: true,
    });
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
      { type: "token", text: "The minimum annual ", replace: false },
      { type: "token", text: "advisory fee is £1,500.", replace: false },
      { type: "token", text: "The minimum annual advisory fee is £1,500.", replace: true },
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

const WAITING: Approval = {
  id: "a-1",
  thread_id: "t-1",
  tool_name: "propose_rebalance",
  tier: "HIGH",
  args: { account_id: "AC-0042" },
  summary: "Raise a rebalance task for account AC-0042.",
  status: "pending",
  requested_by: "adviser-7",
  decided_by: null,
  decision_note: null,
  created_at: "2026-10-19T10:00:00Z",
  decided_at: null,
  consumed_at: null,
  execution: null,
};

const RAN = {
  outcome: "ok" as const,
  at: "2026-10-19T10:05:01Z",
  duration_ms: 42,
  result: { task_id: 103 },
  error: null,
  attempts: 1,
};

/** A research turn as `GET /threads/{id}/turns` sends it. */
const ANSWERED: RestoredTurn = {
  question: "what is the fee floor?",
  answer: "The minimum annual advisory fee is £1,500.",
  route: "research",
  route_confidence: 0.91,
  retrieved_count: 2,
  citations: [{ documentId: "fee-schedule", page: 1, quote: "a minimum annual fee of £1,500" }],
  action_outcome: null,
  approval: null,
  awaiting: [],
  started_at: "2026-10-19T10:00:00Z",
  // A string, as Pydantic serialises the NUMERIC column.
  usage: { input_tokens: 2612, output_tokens: 48, cost_usd: "0.000421", latency_ms: 2814 },
};

describe("restore", () => {
  it("brings a turn back as the same shape the stream builds", () => {
    const turn = restore(ANSWERED, 0);

    expect(turn.question).toBe("what is the fee floor?");
    expect(turn.answer).toBe("The minimum annual advisory fee is £1,500.");
    expect(turn.route).toEqual({ route: "research", confidence: 0.91 });
    expect(turn.retrievedCount).toBe(2);
    expect(turn.citations).toEqual(ANSWERED.citations);
    expect(turn.error).toBeUndefined();
  });

  it("reads the cost as a number, because the wire carries a string", () => {
    // `cost_usd` is a `Decimal` on a NUMERIC column, and Pydantic serialises
    // it as a string so no precision is lost. Carried straight through, the
    // turn renders nothing and the console shows `toFixed is not a function`.
    const turn = restore(ANSWERED, 0);

    expect(turn.cost).toBe(0.000421);
    expect(turn.ms).toBe(2814);
  });

  it("reports no cost rather than zero for a model with no price on file", () => {
    const turn = restore({ ...ANSWERED, usage: { ...ANSWERED.usage!, cost_usd: null } }, 0);

    expect(turn.cost).toBeUndefined();
    // The latency is still a fact, and still shown.
    expect(turn.ms).toBe(2814);
  });

  it("reports no cost at all for a turn that never called a model", () => {
    const turn = restore({ ...ANSWERED, usage: null }, 0);

    expect(turn.cost).toBeUndefined();
    expect(turn.ms).toBeUndefined();
  });

  it("gives each turn an id that cannot collide with one asked in this browser", () => {
    const local = newTurn("asked just now");

    expect(restore(ANSWERED, 0).id).not.toBe(local.id);
    expect(restore(ANSWERED, 1).id).not.toBe(restore(ANSWERED, 0).id);
  });

  it("offers an approval that is still waiting", () => {
    const turn = restore(
      {
        ...ANSWERED,
        answer: null,
        action_outcome: "propose",
        approval: WAITING,
        awaiting: ["approval_gate"],
      },
      0,
    );

    expect(turn.approval).toEqual({
      approvalId: "a-1",
      action: {
        tool: "propose_rebalance",
        tier: "HIGH",
        args: { account_id: "AC-0042" },
        summary: "Raise a rebalance task for account AC-0042.",
      },
    });
    expect(turn.decision).toBeUndefined();
    expect(turn.outcome).toBe("propose");
  });

  it("does not offer a decided approval a second time", () => {
    // The backend answers a second decision with a 409, so a card drawn from
    // a decided approval is a button that cannot work.
    const turn = restore(
      {
        ...ANSWERED,
        answer: null,
        approval: {
          ...WAITING,
          status: "approved",
          decided_by: "adviser-7",
          decided_at: "2026-10-19T10:05:00Z",
          consumed_at: "2026-10-19T10:05:01Z",
          execution: RAN,
        },
        awaiting: [],
      },
      0,
    );

    expect(turn.approval).toBeUndefined();
    expect(turn.decision).toBe("approve");
    expect(turn.executed).toEqual(RAN);
    expect(turn.executionSpent).toBe(true);
  });

  it("does not offer a pending approval the graph is no longer waiting at", () => {
    // Both halves matter: a pending row whose run has moved on cannot be
    // resumed, so the card would raise a decision that resumes nothing.
    const turn = restore({ ...ANSWERED, approval: WAITING, awaiting: [] }, 0);

    expect(turn.approval).toBeUndefined();
    expect(turn.decision).toBeUndefined();
  });

  it("reports an approval that was approved and ran nothing", () => {
    const turn = restore(
      {
        ...ANSWERED,
        approval: { ...WAITING, status: "approved", consumed_at: null, execution: null },
        awaiting: [],
      },
      0,
    );

    expect(turn.decision).toBe("approve");
    expect(turn.executed).toBeNull();
    expect(turn.executionSpent).toBe(false);
  });

  it("claims no decision for an approval that expired", () => {
    // In the schema, written by nothing today. Rendering it as a rejection
    // would put a decision in an adviser's mouth that nobody took.
    const turn = restore({ ...ANSWERED, approval: { ...WAITING, status: "expired" } }, 0);

    expect(turn.decision).toBeUndefined();
    expect(turn.approval).toBeUndefined();
  });

  it("drops an outcome it has no rendering for", () => {
    // The field is `str` on the wire. A value this page does not know would
    // otherwise reach `OutcomeNotice` and be shown as one it does.
    expect(restore({ ...ANSWERED, action_outcome: "something_new" }, 0).outcome).toBeUndefined();
    expect(restore({ ...ANSWERED, action_outcome: "refuse" }, 0).outcome).toBe("refuse");
  });

  it("shows no route rather than a route with no confidence", () => {
    // The route line prints the confidence to two decimal places.
    const turn = restore({ ...ANSWERED, route_confidence: null }, 0);

    expect(turn.route).toBeUndefined();
  });
});
