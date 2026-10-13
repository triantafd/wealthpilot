import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiGet, apiPost } from "./api";

/**
 * A fake Response.
 *
 * `json` is a function rather than a value so a test can make it throw, which
 * is what a non-JSON error body does. One assertion, in one place, rather than
 * a cast at every call site.
 */
function respond(status: number, json: () => Promise<unknown>, ok = false): Response {
  return { ok, status, json } as unknown as Response;
}

const body = (value: unknown) => async () => value;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("error messages", () => {
  it("surfaces the server's detail rather than the status code", async () => {
    // The approvals conflict this exists for: a second reviewer needs to read
    // who decided it first, not decode a 409.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        respond(409, body({ detail: "approval 7f3 is already 'approved', decided by 'reviewer-a'" })),
      ),
    );

    await expect(apiPost("/approvals/7f3", { decision: "reject" })).rejects.toThrow(
      /already 'approved', decided by 'reviewer-a'/,
    );
  });

  it("marks a conflict as one, so a caller can tell a race from a bad request", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(409, body({ detail: "already decided" }))));

    const error = await apiPost("/approvals/7f3", {}).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).isConflict).toBe(true);
    expect((error as ApiError).status).toBe(409);
  });

  it("falls back when the body is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        respond(502, () => {
          throw new Error("not json");
        }),
      ),
    );

    await expect(apiGet("/health")).rejects.toThrow("GET /health failed with 502");
  });

  it("falls back when detail is FastAPI's validation array, not a string", async () => {
    // Rendering this straight would put "[object Object]" in front of someone.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => respond(422, body({ detail: [{ loc: ["body", "decision"], msg: "required" }] }))),
    );

    await expect(apiPost("/approvals/7f3", {})).rejects.toThrow(
      "POST /approvals/7f3 failed with 422",
    );
  });

  it("falls back when there is no detail at all", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(500, body({ error: "boom" }))));

    await expect(apiGet("/usage")).rejects.toThrow("GET /usage failed with 500");
  });
});

describe("success", () => {
  it("returns the parsed body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(200, body({ status: "ok" }), true)));

    await expect(apiGet<{ status: string }>("/health")).resolves.toEqual({ status: "ok" });
  });

  it("posts the body as JSON", async () => {
    const fetchMock = vi.fn(async () => respond(200, body({ ok: true }), true));
    vi.stubGlobal("fetch", fetchMock);

    await apiPost("/approvals/7f3", { decision: "approve", decided_by: "reviewer-a" });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"decision":"approve","decided_by":"reviewer-a"}');
  });
});
