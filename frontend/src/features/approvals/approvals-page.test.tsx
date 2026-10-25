/**
 * The approvals inbox.
 *
 * The provenance assertions are the point: an inbox that lists actions is
 * easy, and an inbox that can say who approved something and what that
 * authorised is what the audit columns were written for.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";

import { ApprovalsPage } from "./approvals-page";

const WAITING = {
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
  ...WAITING,
  id: "a-2",
  status: "approved",
  decided_by: "adviser-7",
  decided_at: "2026-10-19T10:05:00Z",
  consumed_at: "2026-10-19T10:05:01Z",
  execution: {
    outcome: "ok",
    at: "2026-10-19T10:05:01Z",
    duration_ms: 42,
    result: { task_id: 103, account_id: "AC-0042" },
    error: null,
    attempts: 1,
  },
};

const APPROVED_BUT_NEVER_RAN = {
  ...RAN,
  id: "a-3",
  consumed_at: null,
  execution: null,
};

/** Decided before `audit_log.approval_id` was ever written: the approval was
 *  spent, so something ran, and no row can say what. */
const SPENT_BUT_UNRECORDED = {
  ...RAN,
  id: "a-5",
  execution: null,
};

const FAILED = {
  ...RAN,
  id: "a-4",
  execution: {
    outcome: "error",
    at: "2026-10-19T10:05:01Z",
    duration_ms: 12,
    result: null,
    error: "RuntimeError: the task queue is down",
    attempts: 2,
  },
};

function stubFetch(rows: unknown[], options: { decideOk?: boolean } = {}) {
  return vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") {
      return options.decideOk === false
        ? new Response("no", { status: 409 })
        : new Response(JSON.stringify({ approval: rows[0], resumed: true }), { status: 200 });
    }
    return new Response(JSON.stringify(rows), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
}

/** The page inside the providers the app shell gives it: `PageHeader`
 *  renders a `SidebarTrigger`, which reads the sidebar context. */
function renderInbox() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <SidebarProvider>
          <ApprovalsPage />
        </SidebarProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("approvals inbox", () => {
  it("lists what is waiting, with its risk tier", async () => {
    vi.stubGlobal("fetch", stubFetch([WAITING]));
    renderInbox();

    await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());
    expect(screen.getByText("HIGH")).toBeInTheDocument();
    expect(screen.getByText(/Raise a rebalance task/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("says who approved a decided action and what it authorised", async () => {
    vi.stubGlobal("fetch", stubFetch([RAN]));
    renderInbox();

    await waitFor(() => expect(screen.getByTestId("decided-by")).toBeInTheDocument());
    expect(screen.getByTestId("decided-by")).toHaveTextContent("adviser-7");
    expect(screen.getByTestId("outcome")).toHaveTextContent("raised task 103");
    // Decided already: deciding again is the backend's 409, not a button.
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("does not imply an approved action ran when it did not", async () => {
    // The state the durability work made visible: the decision is durable,
    // the execution failed before the tool did anything.
    vi.stubGlobal("fetch", stubFetch([APPROVED_BUT_NEVER_RAN]));
    renderInbox();

    await waitFor(() => expect(screen.getByTestId("outcome")).toBeInTheDocument());
    expect(screen.getByTestId("outcome")).toHaveTextContent(/nothing ran/i);
  });

  it("does not claim nothing ran when the approval was spent", async () => {
    // Every approval decided before the provenance columns existed looks like
    // this. "Nothing ran" would be a false statement about a task that was
    // raised; the inbox says what it can and cannot vouch for.
    vi.stubGlobal("fetch", stubFetch([SPENT_BUT_UNRECORDED]));
    renderInbox();

    await waitFor(() => expect(screen.getByTestId("outcome")).toBeInTheDocument());
    expect(screen.getByTestId("outcome")).toHaveTextContent(/no execution was recorded/i);
  });

  it("shows a failed execution and how often it was offered", async () => {
    vi.stubGlobal("fetch", stubFetch([FAILED]));
    renderInbox();

    await waitFor(() => expect(screen.getByTestId("outcome")).toBeInTheDocument());
    expect(screen.getByTestId("outcome")).toHaveTextContent("the task queue is down");
    expect(screen.getByTestId("outcome")).toHaveTextContent("offered 2 times");
  });

  it("approves from the inbox and refetches", async () => {
    const fetchMock = stubFetch([WAITING]);
    vi.stubGlobal("fetch", fetchMock);
    renderInbox();

    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      const posted = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
      expect(posted?.[0]).toBe("/api/approvals/a-1");
      expect(JSON.parse(String(posted?.[1]?.body))).toMatchObject({ decision: "approve" });
    });
  });

  it("switches filters", async () => {
    const fetchMock = stubFetch([WAITING]);
    vi.stubGlobal("fetch", fetchMock);
    renderInbox();

    await waitFor(() => expect(screen.getByTestId("approval")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("tab", { name: "Approved" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes("status=approved")),
      ).toBe(true),
    );
  });

  it("says so when there is nothing to show", async () => {
    vi.stubGlobal("fetch", stubFetch([]));
    renderInbox();

    await waitFor(() => expect(screen.getByTestId("approvals-empty")).toBeInTheDocument());
  });

  it("reports a failed load rather than an empty inbox", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 500 })),
    );
    renderInbox();

    await waitFor(() => expect(screen.getByTestId("approvals-error")).toBeInTheDocument());
  });
});
