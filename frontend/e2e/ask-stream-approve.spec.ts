/**
 * The whole flow, in a browser, against the real backend.
 *
 * One test rather than several. The steps are not independent — there is no
 * approval to decide until a proposal has been streamed, and nothing to
 * restore until both turns exist — so splitting them would mean either
 * repeating the setup through the UI or sharing state between tests that
 * claim to be separate.
 *
 * What it is here to catch is drift between the page and the API. Every other
 * frontend test writes the server's JSON by hand, so a field the backend
 * renamed stays green everywhere until someone opens the app.
 */

import { expect, test } from "@playwright/test";

/** The account the e2e backend's fake proposes a rebalance for. */
const ACCOUNT = "AC-00001";

test("ask, watch it stream, approve the action, and reload", async ({ page }) => {
  await page.goto("/chat");

  await test.step("the conversation list starts empty", async () => {
    // The database is rebuilt for every run, so this is a fact rather than a
    // hope — and it is what makes the assertions below about *this*
    // conversation meaningful.
    await expect(page.getByText("No conversations yet. Ask something to start one.")).toBeVisible();
  });

  await test.step("a question is answered, with its sources", async () => {
    await page.getByLabel("Message").fill("what does a trade cost?");
    await page.getByRole("button", { name: "Ask" }).click();

    await expect(page.getByTestId("route")).toContainText("research");
    // Provenance before the prose it supports. Pinned at the event level in
    // `test_api_threads.py`; this is the same claim where a reader would meet
    // it, over a real socket rather than a simulated stream.
    await expect(page.getByTestId("route")).toContainText("passages searched");
    await expect(page.getByTestId("answer")).toContainText("set out in the firm's own documents");
    // Verified against what was actually retrieved — the backend drops a
    // citation naming a document nobody searched, so one on screen means the
    // check ran and passed.
    await expect(page.getByTestId("citations")).toBeVisible();
    await expect(page.getByTestId("done")).toContainText("ms");

    // The URL gained the thread's id, which is what makes a reload possible.
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);
  });

  await test.step("an instruction is proposed rather than done", async () => {
    await page.getByLabel("Message").fill(`rebalance ${ACCOUNT}`);
    await page.getByRole("button", { name: "Ask" }).click();

    const card = page.getByTestId("approval");
    await expect(card).toBeVisible();
    await expect(card).toContainText("HIGH");
    await expect(card).toContainText(ACCOUNT);
    // Nothing has run yet: the graph is paused at the gate.
    await expect(page.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  await test.step("approving resumes the graph and runs the tool", async () => {
    await page.getByRole("button", { name: "Approve" }).click();

    await expect(page.getByTestId("decision")).toContainText("Approved");
    // The card is gone, so the same approval cannot be spent twice from the
    // screen that raised it.
    await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);
  });

  await test.step("the conversation survives a reload", async () => {
    // The step that makes this worth running: it goes back through the
    // checkpointer, the approvals table and the usage table at once, and
    // nothing of what is drawn came from this page's memory.
    await page.reload();

    await expect(page.getByTestId("turn")).toHaveCount(2);
    // Scoped to the transcript: the question is also the conversation's label
    // in the sidebar, so an unscoped match finds two and fails.
    await expect(page.getByRole("main").getByText("what does a trade cost?")).toBeVisible();
    await expect(page.getByTestId("answer")).toContainText("set out in the firm's own documents");
    await expect(page.getByTestId("decision")).toContainText("Approved");
  });

  await test.step("the sidebar lists the conversation", async () => {
    const conversations = page.getByRole("navigation", { name: "Conversations" });
    await expect(conversations.getByRole("link")).toHaveCount(1);
    await expect(conversations).toContainText("what does a trade cost?");
  });
});
