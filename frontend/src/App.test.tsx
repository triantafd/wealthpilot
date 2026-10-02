import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "@/App";
import { TooltipProvider } from "@/components/ui/tooltip";

/** A fresh client per test, with retries off so a failed fetch settles at once. */
function renderApp(route = "/") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[route]}>
          <App />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.documentElement.classList.remove("dark");
});

describe("app shell", () => {
  it("renders the sidebar with all five screens", () => {
    renderApp();
    const nav = screen.getByRole("navigation");

    for (const label of ["Chat", "Approvals", "Documents", "Usage", "Evals"]) {
      expect(within(nav).getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("groups navigation by assisting versus inspecting", () => {
    renderApp();
    expect(screen.getByText("Assist")).toBeInTheDocument();
    expect(screen.getByText("Inspect")).toBeInTheDocument();
  });
});

describe("routing", () => {
  it("redirects the root path to chat", () => {
    renderApp("/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Chat");
  });

  it.each([
    ["/chat", "Chat"],
    ["/approvals", "Approvals"],
    ["/documents", "Documents"],
    ["/usage", "Usage"],
    ["/evals", "Evals"],
  ])("renders %s", (route, heading) => {
    renderApp(route);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(heading);
  });

  it("shows a not-found page for an unknown route", () => {
    renderApp("/nope");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
  });

  it("navigates when a sidebar link is clicked", async () => {
    renderApp("/chat");
    const nav = screen.getByRole("navigation");

    await userEvent.click(within(nav).getByRole("link", { name: "Approvals" }));

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Approvals");
  });
});

describe("placeholder screens", () => {
  it("names the phase that will wire each screen up rather than faking data", () => {
    renderApp("/usage");

    expect(screen.getByText(/Phase 2/)).toBeInTheDocument();
    expect(screen.getByText(/not wired up yet/i)).toBeInTheDocument();
    // No invented figures: a dashboard of plausible fake numbers reads as done.
    expect(screen.queryByText(/\$\d/)).not.toBeInTheDocument();
  });
});

describe("backend status", () => {
  it("reports offline when the backend is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));

    renderApp();

    expect(await screen.findByText(/offline/)).toBeInTheDocument();
  });

  it("reports the version and environment when the backend answers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ status: "ok", version: "0.1.0", env: "local" }),
      }),
    );

    renderApp();

    expect(await screen.findByText(/v0\.1\.0 · local/)).toBeInTheDocument();
  });

  it("calls the proxied /api path, not an absolute backend URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "ok", version: "0.1.0", env: "local" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    renderApp();
    await screen.findByText(/v0\.1\.0/);

    expect(fetchMock).toHaveBeenCalledWith("/api/health", expect.anything());
  });
});

describe("theme toggle", () => {
  it("cycles system to light to dark", async () => {
    renderApp();
    const toggle = screen.getByRole("button", { name: /Theme:/ });

    expect(toggle).toHaveAccessibleName(/Theme: system/);

    await userEvent.click(toggle);
    expect(screen.getByRole("button", { name: /Theme: light/ })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Theme: light/ }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });
});
