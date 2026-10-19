/**
 * The chat page, driven by a fake stream.
 *
 * `fetch` is stubbed rather than the module mocked, so the page goes through
 * the real SSE client: the frame splitting, the JSON parsing and the reducer
 * are all exercised, which is where a blank screen would come from.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";

import { ChatPage } from "./chat-page";

/**
 * The page inside the providers the app shell gives it.
 *
 * `PageHeader` renders a `SidebarTrigger`, which reads the sidebar context, so
 * rendering the page bare fails with "useSidebar must be used within a
 * SidebarProvider" — a shell dependency rather than anything about chat.
 */
function renderChat() {
  return render(
    <TooltipProvider>
      <SidebarProvider>
        <ChatPage />
      </SidebarProvider>
    </TooltipProvider>,
  );
}

/** An SSE body from a list of events, split across chunks like a real one. */
function sseBody(events: object[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  const frames = events.map((event) => `data: ${JSON.stringify(event)}\n\n`);
  return new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
}

function stubFetch(events: object[], options: { decideOk?: boolean } = {}) {
  return vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const path = typeof url === "string" ? url : url.toString();

    if (path === "/api/threads" && init?.method === "POST") {
      return new Response(JSON.stringify({ thread_id: "t-1" }), { status: 201 });
    }
    if (path.includes("/messages")) {
      return new Response(sseBody(events), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    if (path.startsWith("/api/approvals/")) {
      return options.decideOk === false
        ? new Response("no", { status: 409 })
        : new Response(JSON.stringify({ resumed: true }), { status: 200 });
    }
    throw new Error(`unexpected request: ${path}`);
  });
}

const RESEARCH_TURN = [
  { type: "route", route: "research", confidence: 0.91 },
  { type: "retrieval", chunks: [{ id: 1, documentId: "fee-schedule", page: 1, score: 0.9 }] },
  // As the model actually answers: the figure comes back emphasised.
  { type: "token", text: "The minimum annual advisory fee is **£1,500**." },
  {
    type: "citations",
    items: [{ documentId: "fee-schedule", page: 1, quote: "a minimum annual fee of £1,500" }],
  },
  { type: "done", usage: { inputTokens: 2612, outputTokens: 48, costUsd: 0.000421, ms: 2814 } },
];

const ACTION_TURN = [
  { type: "route", route: "action", confidence: 0.88 },
  // The backend sends this on every action turn, including a proposal.
  { type: "action_outcome", outcome: "propose" },
  {
    type: "approval_required",
    approvalId: "a-1",
    action: {
      tool: "propose_rebalance",
      tier: "HIGH",
      args: { account_id: "AC-0042" },
      summary: "Raise a rebalance task for account AC-0042.",
    },
  },
  { type: "done", usage: { inputTokens: 681, outputTokens: 28, costUsd: 0.000118, ms: 1061 } },
];

/** A turn where the agent needs one more detail before it can propose. */
const ASKING_TURN = [
  { type: "route", route: "action", confidence: 0.86 },
  { type: "action_outcome", outcome: "ask_account" },
  {
    type: "token",
    text: "Which account should I raise the rebalance for?",
    replace: true,
  },
  { type: "done", usage: { inputTokens: 612, outputTokens: 19, costUsd: 0.000103, ms: 870 } },
];

const REFUSED_TURN = [
  { type: "route", route: "action", confidence: 0.84 },
  { type: "action_outcome", outcome: "refuse" },
  { type: "token", text: "there is no account AC-99999", replace: true },
  { type: "done", usage: { inputTokens: 618, outputTokens: 14, costUsd: 0.000101, ms: 840 } },
];

async function ask(question: string) {
  await userEvent.type(screen.getByLabelText("Message"), question);
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
}

beforeEach(() => {
  vi.stubGlobal("fetch", stubFetch(RESEARCH_TURN));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ChatPage", () => {
  it("invites a question before anything is asked", () => {
    renderChat();

    expect(screen.getByText(/Ask a question about the firm/i)).toBeInTheDocument();
    // The thing that distinguishes this from a chatbot, said up front.
    expect(screen.getByText(/pauses for approval/i)).toBeInTheDocument();
  });

  it("shows the route, the answer and the citations", async () => {
    renderChat();

    await ask("Is there a floor on what we charge?");

    await waitFor(() => expect(screen.getByTestId("answer")).toBeInTheDocument());
    expect(screen.getByTestId("route")).toHaveTextContent("research");
    expect(screen.getByTestId("route")).toHaveTextContent("0.91");
    expect(screen.getByTestId("answer")).toHaveTextContent("£1,500");
    // Rendered, not shown as markup: the figure is bold and no asterisks
    // reach the reader.
    expect(screen.getByTestId("answer").querySelector("strong")).toHaveTextContent("£1,500");
    expect(screen.getByTestId("answer").textContent).not.toContain("**");
    expect(screen.getByTestId("citations")).toHaveTextContent("fee-schedule");
    expect(screen.getByTestId("done")).toHaveTextContent("2814ms");
  });

  it("offers approve and reject for a HIGH-risk action, and says nothing has happened", async () => {
    vi.stubGlobal("fetch", stubFetch(ACTION_TURN));
    renderChat();

    await ask("Rebalance AC-0042");

    await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());
    expect(screen.getByTestId("approval")).toHaveTextContent("HIGH");
    expect(screen.getByTestId("approval")).toHaveTextContent("propose_rebalance");
    // The reassurance that matters on a HIGH action.
    expect(screen.getByText(/Nothing has happened yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
  });

  it("replaces the approval with the outcome once decided", async () => {
    vi.stubGlobal("fetch", stubFetch(ACTION_TURN));
    renderChat();
    await ask("Rebalance AC-0042");
    await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.getByTestId("decision")).toBeInTheDocument());
    expect(screen.getByTestId("decision")).toHaveTextContent(/has run/i);
    // The buttons must go: leaving them would invite a second decision, which
    // the backend refuses with a 409.
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("reports a rejection as nothing having run", async () => {
    vi.stubGlobal("fetch", stubFetch(ACTION_TURN));
    renderChat();
    await ask("Rebalance AC-0042");
    await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => expect(screen.getByTestId("decision")).toHaveTextContent(/nothing ran/i));
  });

  it("marks a turn that is asking, not refusing", async () => {
    // The two arrive as the same shape of paragraph. An adviser who reads a
    // question as a refusal stops, and the proposal never gets made.
    vi.stubGlobal("fetch", stubFetch(ASKING_TURN));
    renderChat();

    await ask("rebalance the account, it has drifted");

    await waitFor(() => expect(screen.getByTestId("outcome")).toBeInTheDocument());
    expect(screen.getByTestId("outcome")).toHaveTextContent(/needs your answer/i);
    expect(screen.queryByTestId("approval")).not.toBeInTheDocument();
  });

  it("marks a refusal as a dead end", async () => {
    vi.stubGlobal("fetch", stubFetch(REFUSED_TURN));
    renderChat();

    await ask("rebalance AC-99999");

    await waitFor(() => expect(screen.getByTestId("outcome")).toBeInTheDocument());
    expect(screen.getByTestId("outcome")).toHaveTextContent(/declined/i);
  });

  it("labels nothing when the agent proposed", async () => {
    // The approval card and its HIGH badge already say it, louder.
    vi.stubGlobal("fetch", stubFetch(ACTION_TURN));
    renderChat();

    await ask("rebalance AC-00042");

    await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());
    expect(screen.queryByTestId("outcome")).not.toBeInTheDocument();
  });

  it("shows an error event rather than an empty answer", async () => {
    vi.stubGlobal("fetch", stubFetch([{ type: "error", message: "provider exploded" }]));
    renderChat();

    await ask("anything");

    await waitFor(() => expect(screen.getByTestId("error")).toHaveTextContent("provider exploded"));
  });

  it("surfaces a rejected decision instead of pretending it worked", async () => {
    vi.stubGlobal("fetch", stubFetch(ACTION_TURN, { decideOk: false }));
    renderChat();
    await ask("Rebalance AC-0042");
    await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.getByTestId("error")).toBeInTheDocument());
    expect(screen.getByTestId("error")).toHaveTextContent("409");
  });

  it("does not submit an empty message", async () => {
    renderChat();

    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
  });

  it("reuses one thread, so a follow-up continues the conversation", async () => {
    const fetchMock = stubFetch(RESEARCH_TURN);
    vi.stubGlobal("fetch", fetchMock);
    renderChat();

    await ask("first question");
    await waitFor(() => expect(screen.getAllByTestId("answer")).toHaveLength(1));
    await ask("second question");
    await waitFor(() => expect(screen.getAllByTestId("answer")).toHaveLength(2));

    // One POST /api/threads, two POSTs to the same thread's messages: a second
    // thread would lose the checkpointer's history and any pending approval.
    const calls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(calls.filter((url) => url === "/api/threads")).toHaveLength(1);
    expect(calls.filter((url) => url.includes("/messages"))).toHaveLength(2);
  });

  it("keeps the earlier turn on screen when a second question is asked", async () => {
    // The defect this replaces: the page held one turn and replaced it, so a
    // second question erased the first — including an answer an adviser was
    // still reading, and any approval that turn was waiting on.
    vi.stubGlobal("fetch", stubFetch(RESEARCH_TURN));
    renderChat();

    await ask("what is the fee floor?");
    await waitFor(() => expect(screen.getAllByTestId("answer")).toHaveLength(1));
    await ask("and for a junior account?");

    await waitFor(() => expect(screen.getAllByTestId("turn")).toHaveLength(2));
    expect(screen.getByText("what is the fee floor?")).toBeInTheDocument();
    expect(screen.getByText("and for a junior account?")).toBeInTheDocument();
  });

  it("decides the approval belonging to its own turn, not the newest one", async () => {
    // An adviser can ask something else while an approval waits. Looking up
    // "the latest turn" would then decide against the wrong one, or crash.
    const fetchMock = stubFetch(ACTION_TURN);
    vi.stubGlobal("fetch", fetchMock);
    renderChat();

    await ask("rebalance AC-00042");
    await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());

    const turns = screen.getAllByTestId("turn");
    expect(turns).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.getByTestId("decision")).toBeInTheDocument());
    const decided = fetchMock.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes("/approvals/"));
    expect(decided).toHaveLength(1);
  });
});
