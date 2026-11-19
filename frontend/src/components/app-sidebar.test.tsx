/**
 * The conversations list.
 *
 * `fetch` is stubbed rather than the hook mocked, so the query, the `select`
 * that unwraps the envelope and the row rendering are all exercised — a list
 * that silently shows nothing is the failure this guards.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppSidebar } from "@/components/app-sidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { ThreadSummary } from "@/lib/api";

function thread(id: string, question: string): ThreadSummary {
  return {
    thread_id: id,
    first_question: question,
    created_at: "2026-11-19T10:00:00Z",
    last_active_at: "2026-11-19T10:00:00Z",
    user_id: "adviser-7",
  };
}

/** `fetch` answering by path: the footer asks for health, the list for threads. */
function stubFetch(threads: ThreadSummary[] | "pending" | "error") {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: unknown) => {
      if (!String(input).startsWith("/api/threads")) {
        return Promise.reject(new Error("not this test's request"));
      }
      if (threads === "error") return Promise.reject(new Error("down"));
      // A promise that never settles is how "still loading" is held still.
      if (threads === "pending") return new Promise(() => {});
      return Promise.resolve({ ok: true, json: async () => ({ threads }) });
    }),
  );
}

function renderSidebar(route = "/chat") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <SidebarProvider>
          <MemoryRouter initialEntries={[route]}>
            <AppSidebar />
          </MemoryRouter>
        </SidebarProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

/** The list's own landmark, so a row is not confused with a screen link. */
function list() {
  return screen.getByRole("navigation", { name: "Conversations" });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the conversations list", () => {
  it("renders a row per thread, labelled with its first question", async () => {
    stubFetch([thread("t-1", "what does a trade cost?"), thread("t-2", "rebalance AC-00037")]);
    renderSidebar();

    const first = await within(list()).findByRole("link", { name: "what does a trade cost?" });
    expect(first).toHaveAttribute("href", "/chat/t-1");
    expect(
      within(list()).getByRole("link", { name: "rebalance AC-00037" }),
    ).toHaveAttribute("href", "/chat/t-2");
  });

  it("keeps the order the server sent, rather than sorting it again", async () => {
    // The backend orders by `last_active_at`; re-sorting here on anything else
    // would quietly disagree with it.
    stubFetch([thread("t-1", "most recent"), thread("t-2", "older"), thread("t-3", "oldest")]);
    renderSidebar();

    await within(list()).findByRole("link", { name: "most recent" });
    const labels = within(list())
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(labels).toEqual(["most recent", "older", "oldest"]);
  });

  it("marks the conversation the page is on", async () => {
    stubFetch([thread("t-1", "the open one"), thread("t-2", "another")]);
    renderSidebar("/chat/t-1");

    const open = await within(list()).findByRole("link", { name: "the open one" });
    expect(open).toHaveAttribute("aria-current", "page");
    expect(within(list()).getByRole("link", { name: "another" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("keeps the whole question in the row, and truncates it in CSS", async () => {
    // Cut in JavaScript and the tooltip and a screen reader get the stub too.
    const long = ["which accounts hold MER011", ...Array(10).fill("and anything else")].join(" ");
    stubFetch([thread("t-1", long)]);
    renderSidebar();

    const row = await within(list()).findByRole("link", { name: long });
    expect(within(row).getByText(long)).toHaveClass("truncate");
  });

  it("says so when there are no conversations rather than showing an empty list", async () => {
    stubFetch([]);
    renderSidebar();

    expect(await within(list()).findByText(/No conversations yet/)).toBeInTheDocument();
    expect(within(list()).queryAllByRole("link")).toHaveLength(0);
  });

  it("says so when the list could not be loaded", async () => {
    stubFetch("error");
    renderSidebar();

    expect(await within(list()).findByText(/could not be loaded/)).toBeInTheDocument();
  });

  it("shows placeholders while the list is in flight, not an empty-state lie", async () => {
    stubFetch("pending");
    const { container } = renderSidebar();

    expect(container.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0);
    expect(within(list()).queryByText(/No conversations yet/)).not.toBeInTheDocument();
  });
});

describe("the new-chat link", () => {
  it("is not marked current while a conversation is open", async () => {
    // NavLink matches by prefix, so `/chat` was "active" on `/chat/{id}` too.
    // Harmless until the list arrived, and then two rows said "you are here".
    stubFetch([thread("t-1", "the open one")]);
    renderSidebar("/chat/t-1");

    await within(list()).findByRole("link", { name: "the open one" });
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "New chat" })).not.toHaveAttribute(
      "aria-current",
    );
  });


  it("says what it does, now that leaving a thread is possible", async () => {
    // `/chat` with no id is a new conversation. Labelled "Chat" beside a list
    // of conversations, it reads as "the chat screen" and loses the adviser's
    // place.
    stubFetch([]);
    renderSidebar();

    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "New chat" })).toHaveAttribute("href", "/chat");
  });
});
