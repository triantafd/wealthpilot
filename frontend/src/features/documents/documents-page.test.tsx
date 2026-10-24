/**
 * The documents page.
 *
 * `fetch` is stubbed rather than the module mocked, so the page goes through
 * the real `apiGet` — including the error path, where the server's `detail`
 * is what a reader is shown.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";

import { DocumentsPage } from "./documents-page";

const FEES = {
  id: "fee-schedule",
  title: "Fee schedule",
  chunk_count: 12,
  page_count: 4,
  version: 1,
  ingested_at: "2026-10-09T19:12:00Z",
};

const EMPTY_DOCUMENT = {
  ...FEES,
  id: "mandates-and-rebalancing",
  title: "Mandates and rebalancing",
  chunk_count: 0,
  page_count: 2,
};

function stubFetch(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

/** The page inside the providers the app shell gives it: `PageHeader` renders
 *  a `SidebarTrigger`, which reads the sidebar context. */
function renderDocuments() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <SidebarProvider>
          <DocumentsPage />
        </SidebarProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("DocumentsPage", () => {
  it("lists what is indexed, with the slug a citation prints", async () => {
    vi.stubGlobal("fetch", stubFetch([FEES]));
    renderDocuments();

    await waitFor(() => expect(screen.getByTestId("document")).toBeInTheDocument());
    expect(screen.getByText("Fee schedule")).toBeInTheDocument();
    // The id, not just the title: an answer cites `fee-schedule`, and
    // matching it to this list is the reason to be on this page.
    expect(screen.getByText("fee-schedule")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("9 Oct 2026")).toBeInTheDocument();
  });

  it("calls out a document the agent cannot cite", async () => {
    // The row the page exists for. A quiet `0` in a column of numbers is
    // exactly as easy to miss as the psql query nobody ran.
    vi.stubGlobal("fetch", stubFetch([EMPTY_DOCUMENT]));
    renderDocuments();

    await waitFor(() => expect(screen.getByTestId("not-searchable")).toBeInTheDocument());
    expect(screen.getByTestId("not-searchable")).toHaveTextContent("not searchable");
  });

  it("totals the corpus, so a half-ingested one is visible", async () => {
    vi.stubGlobal("fetch", stubFetch([FEES, EMPTY_DOCUMENT]));
    renderDocuments();

    await waitFor(() => expect(screen.getAllByTestId("document")).toHaveLength(2));
    expect(screen.getByTestId("corpus-total")).toHaveTextContent("2 documents · 12 passages");
  });

  it("names the command to run when nothing is indexed", async () => {
    // An empty corpus is not an error and not a blank page: it is a step
    // nobody has taken, and the page says which one.
    vi.stubGlobal("fetch", stubFetch([]));
    renderDocuments();

    await waitFor(() => expect(screen.getByTestId("documents-empty")).toBeInTheDocument());
    expect(screen.getByTestId("documents-empty")).toHaveTextContent("app.scripts.ingest");
    expect(screen.queryByTestId("document")).not.toBeInTheDocument();
  });

  it("says why the corpus is missing rather than showing an empty one", async () => {
    // The two look identical on screen and mean opposite things: nothing
    // ingested, versus a backend that is not answering.
    vi.stubGlobal("fetch", stubFetch({ detail: "the database is not available" }, 503));
    renderDocuments();

    await waitFor(() => expect(screen.getByTestId("documents-failed")).toBeInTheDocument());
    expect(screen.getByTestId("documents-failed")).toHaveTextContent("database is not available");
    expect(screen.queryByTestId("documents-empty")).not.toBeInTheDocument();
  });

  it("shows a placeholder while the corpus is loading", async () => {
    vi.stubGlobal("fetch", stubFetch([FEES]));
    renderDocuments();

    expect(screen.getByTestId("documents-loading")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("document")).toBeInTheDocument());
  });
});
