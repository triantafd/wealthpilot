/**
 * The browser test: the built app, a real backend, a real Postgres.
 *
 * What this buys over the 160 jsdom tests, which is the only reason to pay
 * for a browser at all: those stub `fetch` with JSON written by hand, so the
 * page's idea of the API and the API itself can drift apart without anything
 * going red. Here nothing is stubbed. It also exercises SSE over a real
 * socket, which jsdom only imitates, and proves `pnpm build` produces
 * something that runs.
 *
 * Two servers, started in order by Playwright and left running between tests.
 * The backend is `backend/tests/e2e/server.py`, which rebuilds its own
 * database and substitutes a fake model and embedder — see that file for why
 * those two and nothing else.
 */

import { defineConfig, devices } from "@playwright/test";

/** The e2e backend's port. Not 8000: a developer's own backend may be there,
 *  and a test that quietly ran against it would write to their data. */
const API_PORT = 8123;
const APP_PORT = 4173;

export default defineConfig({
  testDir: "./e2e",
  // One worker. The backend holds a single checkpointer connection and the
  // test asserts on the whole conversation list, so two specs sharing it
  // would see each other's threads.
  workers: 1,
  fullyParallel: false,
  // A failure here is a real failure. A retry would turn an intermittent bug
  // — the kind an SSE test exists to find — into a green run.
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],

  use: {
    baseURL: `http://localhost:${APP_PORT}`,
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  webServer: [
    {
      // From `backend/`, because the launcher is `tests.e2e.server` on that
      // package path and uv resolves the project from the working directory.
      command: "uv run python -m tests.e2e.server",
      cwd: "../backend",
      url: `http://127.0.0.1:${API_PORT}/health`,
      // Generous: this command drops and recreates a database, runs every
      // migration, seeds a firm and indexes the corpus before it serves
      // anything. Measured at around 45 seconds on a laptop.
      timeout: 180_000,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      // The production build, not the dev server: a test against `vite dev`
      // says nothing about what is deployed.
      command: `E2E_API_PORT=${API_PORT} pnpm build && E2E_API_PORT=${API_PORT} pnpm preview --port ${APP_PORT}`,
      url: `http://localhost:${APP_PORT}`,
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
});
