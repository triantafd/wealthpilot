/**
 * The chat page, driven by a fake stream.
 *
 * `fetch` is stubbed rather than the module mocked, so the page goes through
 * the real SSE client: the frame splitting, the JSON parsing and the reducer
 * are all exercised, which is where a blank screen would come from.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";

import { ChatPage } from "./chat-page";

/** A test's view of the address bar: the thread id is part of the URL now. */
function Address() {
  return <span data-testid="address">{useLocation().pathname}</span>;
}

/**
 * The page inside the providers the app shell gives it.
 *
 * `PageHeader` renders a `SidebarTrigger`, which reads the sidebar context, so
 * rendering the page bare fails with "useSidebar must be used within a
 * SidebarProvider" — a shell dependency rather than anything about chat.
 *
 * Both chat routes are mounted, because the page reads its thread id with
 * `useParams` and the two cases — with an id and without — are different
 * behaviour. That the app wires these paths to this page is pinned in
 * `App.test.tsx` instead, so a route renamed in one place fails somewhere.
 *
 * The two links are the two ways an adviser leaves a conversation without the
 * component unmounting, which is the case the page has to tell apart from its
 * own navigation.
 *
 * A fresh `QueryClient` per test, so one test's restored thread is not still
 * in the cache for the next one. `retry: false` because a test that stubs a
 * 404 should see it once, not after three backed-off attempts.
 */
function renderChat(route = "/chat") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <SidebarProvider>
          <MemoryRouter initialEntries={[route]}>
            <Routes>
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/chat/:threadId" element={<ChatPage />} />
            </Routes>
            <Address />
            <Link to="/chat/t-elsewhere">another thread</Link>
            <Link to="/chat">new chat</Link>
          </MemoryRouter>
        </SidebarProvider>
      </TooltipProvider>
    </QueryClientProvider>,
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

const RAN = {
  outcome: "ok",
  at: "2026-10-19T10:05:01Z",
  duration_ms: 42,
  result: { task_id: 103, account_id: "AC-0042" },
  error: null,
  attempts: 1,
};

const DECIDED_APPROVAL = {
  id: "a-1",
  thread_id: "t-1",
  tool_name: "propose_rebalance",
  tier: "HIGH",
  args: { account_id: "AC-0042" },
  summary: "Raise a rebalance task for account AC-0042.",
  status: "approved",
  requested_by: null,
  decided_by: "adviser-7",
  decision_note: null,
  created_at: "2026-10-19T10:00:00Z",
  decided_at: "2026-10-19T10:05:00Z",
  consumed_at: "2026-10-19T10:05:01Z",
  execution: RAN,
};

/** A research turn as `GET /threads/{id}/turns` sends it: snake_case, with
 *  `cost_usd` a string because the column is NUMERIC. */
const RESTORED_ANSWER = {
  question: "what did we charge last year?",
  answer: "The minimum annual advisory fee is **£1,500**.",
  route: "research",
  route_confidence: 0.91,
  retrieved_count: 1,
  citations: [{ documentId: "fee-schedule", page: 1, quote: "a minimum annual fee of £1,500" }],
  action_outcome: null,
  approval: null,
  awaiting: [],
  started_at: "2026-10-19T10:00:00Z",
  usage: { input_tokens: 2612, output_tokens: 48, cost_usd: "0.000421", latency_ms: 2814 },
};

/** A turn reloaded while its approval is still waiting at the gate. */
const RESTORED_WAITING = {
  ...RESTORED_ANSWER,
  question: "rebalance AC-0042",
  answer: null,
  route: "action",
  route_confidence: 0.88,
  retrieved_count: 0,
  citations: [],
  action_outcome: "propose",
  approval: {
    ...DECIDED_APPROVAL,
    status: "pending",
    decided_by: null,
    decided_at: null,
    consumed_at: null,
    execution: null,
  },
  awaiting: ["approval_gate"],
};

/** The same turn after the approval was decided and the tool ran. */
const RESTORED_DECIDED = {
  ...RESTORED_WAITING,
  approval: DECIDED_APPROVAL,
  awaiting: [],
};

interface StubOptions {
  decideOk?: boolean;
  execution?: Record<string, unknown> | null;
  /** Restored turns per thread id. A thread not named here has none, which is
   *  what every test about streaming wants. */
  turns?: Record<string, object[]>;
  /** For the thread that cannot be restored: the endpoint 404s an id the
   *  checkpointer has never seen. */
  turnsStatus?: number;
  truncated?: boolean;
  /** Held until the test resolves it, to pin what happens to a question asked
   *  while the history is still in flight. */
  turnsGate?: Promise<void>;
}

function stubFetch(events: object[], options: StubOptions = {}) {
  return vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const path = typeof url === "string" ? url : url.toString();

    if (path === "/api/threads" && init?.method === "POST") {
      return new Response(JSON.stringify({ thread_id: "t-1" }), { status: 201 });
    }
    if (path.endsWith("/turns")) {
      await options.turnsGate;
      const thread = path.replace("/api/threads/", "").replace("/turns", "");
      if (options.turnsStatus) {
        return new Response(JSON.stringify({ detail: `no thread '${thread}'` }), {
          status: options.turnsStatus,
        });
      }
      return new Response(
        JSON.stringify({
          thread_id: thread,
          turns: options.turns?.[thread] ?? [],
          truncated: options.truncated ?? false,
        }),
        { status: 200 },
      );
    }
    if (path.includes("/messages")) {
      return new Response(sseBody(events), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    if (path.startsWith("/api/approvals/")) {
      const decision = JSON.parse(String(init?.body)).decision as "approve" | "reject";
      return options.decideOk === false
        ? new Response("no", { status: 409 })
        : new Response(
            // The shape `POST /approvals/{id}` really answers with. It used
            // to be stubbed as `{resumed: true}` alone, which let the page
            // read a field the endpoint had never returned.
            JSON.stringify({
              resumed: true,
              approval: {
                ...DECIDED_APPROVAL,
                status: decision === "reject" ? "rejected" : "approved",
                consumed_at: decision === "reject" ? null : "2026-10-19T10:05:01Z",
                // `in` rather than `??`: an explicitly passed `null` is the
                // case under test, and `null ?? RAN` would quietly be RAN.
                execution:
                  decision === "reject" ? null : "execution" in options ? options.execution : RAN,
              },
            }),
            { status: 200 },
          );
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
    // What it did, not that it did something: the task it raised.
    expect(screen.getByTestId("decision")).toHaveTextContent(/raised task 103/i);
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

  it("does not claim an approved action ran when it did not", async () => {
    // A real state, not a hypothetical: the decision is durable and the
    // execution can still fail before the tool does anything. "The action has
    // run" was a fixed string that said otherwise every time.
    vi.stubGlobal("fetch", stubFetch(ACTION_TURN, { execution: null }));
    renderChat();

    await ask("rebalance AC-0042");
    await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(screen.getByTestId("decision")).toHaveTextContent(/no execution was recorded/i),
    );
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

  describe("the thread id in the URL", () => {
    it("puts the new thread's id in the address, so a reload can find it again", async () => {
      vi.stubGlobal("fetch", stubFetch(RESEARCH_TURN));
      renderChat();

      expect(screen.getByTestId("address")).toHaveTextContent("/chat");
      await ask("what is the fee floor?");

      await waitFor(() => expect(screen.getByTestId("address")).toHaveTextContent("/chat/t-1"));
    });

    it("keeps the answer on screen while the address gains the thread id", async () => {
      // The regression the `driving` ref exists to prevent. The page stays
      // mounted across `/chat` -> `/chat/t-1`, so an effect that treated any
      // change of the id as a new conversation would wipe the turn that is
      // still streaming into it — at the exact moment the first answer lands.
      vi.stubGlobal("fetch", stubFetch(RESEARCH_TURN));
      renderChat();

      await ask("what is the fee floor?");

      await waitFor(() => expect(screen.getByTestId("address")).toHaveTextContent("/chat/t-1"));
      expect(screen.getByTestId("answer")).toBeInTheDocument();
      expect(screen.getAllByTestId("turn")).toHaveLength(1);
      expect(screen.getByText("what is the fee floor?")).toBeInTheDocument();
    });

    it("continues the thread the URL names rather than starting another", async () => {
      // What makes a reload worth anything: the conversation the checkpointer
      // has, not a new one beside it.
      const fetchMock = stubFetch(RESEARCH_TURN);
      vi.stubGlobal("fetch", fetchMock);
      renderChat("/chat/t-restored");

      await ask("and for a junior account?");
      await waitFor(() => expect(screen.getByTestId("answer")).toBeInTheDocument());

      const calls = fetchMock.mock.calls.map(([url]) => String(url));
      expect(calls.filter((url) => url === "/api/threads")).toHaveLength(0);
      expect(calls).toContain("/api/threads/t-restored/messages");
    });

    it("clears the transcript when another conversation is opened", async () => {
      vi.stubGlobal("fetch", stubFetch(RESEARCH_TURN));
      renderChat("/chat/t-restored");

      await ask("what is the fee floor?");
      await waitFor(() => expect(screen.getByTestId("answer")).toBeInTheDocument());
      await userEvent.click(screen.getByRole("link", { name: "another thread" }));

      await waitFor(() => expect(screen.queryByTestId("turn")).not.toBeInTheDocument());
      expect(screen.getByTestId("address")).toHaveTextContent("/chat/t-elsewhere");
    });

    it("starts a fresh conversation when the Chat link is clicked", async () => {
      // The sidebar's own link. The id disappearing is a new conversation, so
      // the next question must open a thread rather than carry on in the old
      // one — which would append to a transcript the adviser cannot see.
      const fetchMock = stubFetch(RESEARCH_TURN);
      vi.stubGlobal("fetch", fetchMock);
      renderChat("/chat/t-restored");

      await ask("what is the fee floor?");
      await waitFor(() => expect(screen.getByTestId("answer")).toBeInTheDocument());
      await userEvent.click(screen.getByRole("link", { name: "new chat" }));
      await waitFor(() => expect(screen.queryByTestId("turn")).not.toBeInTheDocument());
      await ask("a question in a new conversation");

      await waitFor(() => expect(screen.getByTestId("address")).toHaveTextContent("/chat/t-1"));
      const calls = fetchMock.mock.calls.map(([url]) => String(url));
      expect(calls.filter((url) => url === "/api/threads")).toHaveLength(1);
      expect(calls).toContain("/api/threads/t-1/messages");
    });
  });
  describe("restoring a conversation", () => {
    it("brings back the questions, answers, citations and cost of a reloaded thread", async () => {
      // The refresh case, end to end: nothing is typed, and the conversation
      // is on screen with the evidence that makes a restored answer
      // trustworthy rather than merely present.
      vi.stubGlobal(
        "fetch",
        stubFetch(RESEARCH_TURN, { turns: { "t-restored": [RESTORED_ANSWER] } }),
      );
      renderChat("/chat/t-restored");

      await waitFor(() => expect(screen.getByTestId("answer")).toBeInTheDocument());
      expect(screen.getByText("what did we charge last year?")).toBeInTheDocument();
      expect(screen.getByTestId("route")).toHaveTextContent("research");
      expect(screen.getByTestId("route")).toHaveTextContent("0.91");
      // Restored through the same renderer as a streamed answer, so the
      // emphasis is rendered and no asterisks reach the reader.
      expect(screen.getByTestId("answer").querySelector("strong")).toHaveTextContent("£1,500");
      expect(screen.getByTestId("citations")).toHaveTextContent("fee-schedule");
      expect(screen.getByTestId("done")).toHaveTextContent("2814ms");
      expect(screen.getByTestId("done")).toHaveTextContent("$0.000421");
      expect(screen.queryByTestId("restoring")).not.toBeInTheDocument();
    });

    it("lets a restored approval be decided, since the gate is still waiting", async () => {
      // The reason the endpoint returns `awaiting`. Reloading mid-approval has
      // to leave a card an adviser can act on, not a decision nobody took.
      vi.stubGlobal(
        "fetch",
        stubFetch(ACTION_TURN, { turns: { "t-restored": [RESTORED_WAITING] } }),
      );
      renderChat("/chat/t-restored");

      await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());
      expect(screen.getByTestId("approval")).toHaveTextContent("HIGH");
      expect(screen.getByTestId("approval")).toHaveTextContent("propose_rebalance");

      await userEvent.click(screen.getByRole("button", { name: "Approve" }));

      await waitFor(() => expect(screen.getByTestId("decision")).toBeInTheDocument());
      expect(screen.getByTestId("decision")).toHaveTextContent(/raised task 103/i);
    });

    it("says what a decided turn did rather than offering it again", async () => {
      // A second decision is a 409, so buttons here are a dead end — and the
      // adviser needs the task number, not the question a second time.
      vi.stubGlobal(
        "fetch",
        stubFetch(ACTION_TURN, { turns: { "t-restored": [RESTORED_DECIDED] } }),
      );
      renderChat("/chat/t-restored");

      await waitFor(() => expect(screen.getByTestId("decision")).toBeInTheDocument());
      expect(screen.getByTestId("decision")).toHaveTextContent(/raised task 103/i);
      expect(screen.queryByTestId("approval")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    });

    it("says a conversation could not be restored instead of showing an empty one", async () => {
      // An empty chat reads as a conversation nobody ever had. The id in the
      // URL says otherwise, and the difference matters: one invites a
      // question, the other says the history is missing.
      vi.stubGlobal("fetch", stubFetch(RESEARCH_TURN, { turnsStatus: 404 }));
      renderChat("/chat/t-gone");

      await waitFor(() => expect(screen.getByTestId("restore-failed")).toBeInTheDocument());
      expect(screen.getByTestId("restore-failed")).toHaveTextContent("could not be restored");
      // The server's own words, which name the thread it could not find.
      expect(screen.getByTestId("restore-failed")).toHaveTextContent("no thread 't-gone'");
      expect(screen.queryByText(/Ask a question about the firm/i)).not.toBeInTheDocument();
      // Still usable: the composer is how an adviser carries on from here.
      expect(screen.getByLabelText("Message")).toBeEnabled();
    });

    it("asks for no history for a conversation it has just started", async () => {
      // There is nothing to restore, and the request would race the stream
      // writing the very turn it would be asking about.
      const fetchMock = stubFetch(RESEARCH_TURN);
      vi.stubGlobal("fetch", fetchMock);
      renderChat();

      await ask("a question in a brand new conversation");
      await waitFor(() => expect(screen.getByTestId("address")).toHaveTextContent("/chat/t-1"));
      await waitFor(() => expect(screen.getByTestId("answer")).toBeInTheDocument());

      const calls = fetchMock.mock.calls.map(([url]) => String(url));
      expect(calls.filter((url) => url.endsWith("/turns"))).toHaveLength(0);
    });

    it("keeps a question asked while the history was still arriving", async () => {
      // Why the restored turns are prepended rather than assigned: they are
      // older than anything typed here, and assigning would delete the answer
      // the adviser is reading at the moment the fetch lands.
      let open = () => {};
      const gate = new Promise<void>((resolve) => {
        open = resolve;
      });
      vi.stubGlobal(
        "fetch",
        stubFetch(RESEARCH_TURN, { turns: { "t-restored": [RESTORED_ANSWER] }, turnsGate: gate }),
      );
      renderChat("/chat/t-restored");

      await ask("asked before the history came back");
      await waitFor(() => expect(screen.getByTestId("answer")).toBeInTheDocument());
      open();

      await waitFor(() => expect(screen.getAllByTestId("turn")).toHaveLength(2));
      // Oldest first, as the conversation happened.
      const asked = screen.getAllByTestId("turn").map((turn) => turn.textContent);
      expect(asked[0]).toContain("what did we charge last year?");
      expect(asked[1]).toContain("asked before the history came back");
    });

    it("restores the other thread's history when another conversation is opened", async () => {
      vi.stubGlobal(
        "fetch",
        stubFetch(RESEARCH_TURN, {
          turns: { "t-restored": [RESTORED_ANSWER], "t-elsewhere": [RESTORED_WAITING] },
        }),
      );
      renderChat("/chat/t-restored");
      await waitFor(() => expect(screen.getByTestId("answer")).toBeInTheDocument());

      await userEvent.click(screen.getByRole("link", { name: "another thread" }));

      await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());
      expect(screen.getByText("rebalance AC-0042")).toBeInTheDocument();
      // The thread that was open is gone, not merged into the new one.
      expect(screen.queryByText("what did we charge last year?")).not.toBeInTheDocument();
      expect(screen.getAllByTestId("turn")).toHaveLength(1);
    });

    it("restores a thread again after the Chat link was used", async () => {
      // The history is cached, so the guard against restoring twice has to be
      // cleared when the transcript is. Otherwise going back to a thread
      // leaves it blank with its own history sitting in the cache.
      vi.stubGlobal(
        "fetch",
        stubFetch(RESEARCH_TURN, { turns: { "t-elsewhere": [RESTORED_ANSWER] } }),
      );
      renderChat("/chat/t-elsewhere");
      await waitFor(() => expect(screen.getByTestId("answer")).toBeInTheDocument());

      await userEvent.click(screen.getByRole("link", { name: "new chat" }));
      await waitFor(() => expect(screen.queryByTestId("turn")).not.toBeInTheDocument());
      await userEvent.click(screen.getByRole("link", { name: "another thread" }));

      await waitFor(() => expect(screen.getByTestId("answer")).toBeInTheDocument());
      expect(screen.getAllByTestId("turn")).toHaveLength(1);
    });

    it("says when a conversation is too long to restore in full", async () => {
      // The endpoint stops at a bound. A transcript silently missing its
      // start would have the adviser reading a conversation that begins in
      // the middle without knowing it.
      vi.stubGlobal(
        "fetch",
        stubFetch(RESEARCH_TURN, {
          turns: { "t-restored": [RESTORED_ANSWER] },
          truncated: true,
        }),
      );
      renderChat("/chat/t-restored");

      await waitFor(() => expect(screen.getByTestId("truncated")).toBeInTheDocument());
      expect(screen.getByTestId("truncated")).toHaveTextContent(/oldest turns are not shown/i);
    });
  });
});
