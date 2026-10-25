/**
 * Typed fetch wrapper.
 *
 * Requests go to relative `/api/...` paths, which Vite proxies to the backend
 * on port 8000 in development (see vite.config.ts). That keeps the base URL out
 * of the code entirely, so there is no environment variable to get wrong.
 *
 * The SSE client for streaming agent events is separate and arrives with the
 * graph in Phase 4; this is for ordinary JSON endpoints.
 *
 * Errors carry the server's own message. FastAPI puts it in `detail`, and
 * discarding it leaves the UI showing "failed with 409" where the backend said
 * which reviewer decided the approval first — a conflict a second reviewer
 * needs to read, not a status code to decode.
 */

import type { Citation } from "@shared/stream-events";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }

  /** True when the request lost a race rather than being malformed. */
  get isConflict(): boolean {
    return this.status === 409;
  }
}

/**
 * The server's message, or a fallback naming the request.
 *
 * A failed response may carry no body, a non-JSON body, or a `detail` that is
 * FastAPI's validation array rather than a string — so each of those falls
 * back rather than rendering "[object Object]" at someone.
 */
async function errorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const { detail } = body as { detail: unknown };
      if (typeof detail === "string" && detail.trim()) {
        return detail;
      }
    }
  } catch {
    // No body, or not JSON. The fallback is already correct.
  }
  return fallback;
}

/** GET a JSON endpoint. Throws ApiError on a non-2xx response. */
export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { Accept: "application/json" },
    signal,
  });

  if (!response.ok) {
    throw new ApiError(
      response.status,
      await errorMessage(response, `GET ${path} failed with ${response.status}`),
    );
  }

  return (await response.json()) as T;
}

/** POST a JSON body. Throws ApiError carrying the server's message on a non-2xx. */
export async function apiPost<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    throw new ApiError(
      response.status,
      await errorMessage(response, `POST ${path} failed with ${response.status}`),
    );
  }

  return (await response.json()) as T;
}

/** Shape of `GET /health`, mirroring the Health model in app/main.py. */
export interface Health {
  status: "ok";
  version: string;
  env: string;
}

/** What an approval authorised, once it was spent.
 *
 *  Absent for three different reasons, and they are not the same statement: a
 *  rejection ran nothing, an approval can be decided and still never execute,
 *  and one spent before `audit_log.approval_id` was written ran something no
 *  row can name. `ExecutionOutcome` is where that is turned into words. */
export interface Execution {
  outcome: "ok" | "error" | "denied";
  at: string;
  duration_ms: number | null;
  result: Record<string, unknown> | null;
  error: string | null;
  attempts: number;
}

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

/** One approval, as `/approvals` and `POST /approvals/{id}` return it.
 *
 *  Hand-written rather than shared, unlike the SSE contract in
 *  `shared/stream-events.ts`: these are REST models and they are snake_case
 *  on the wire, so the names here are the backend's own. */
export interface Approval {
  id: string;
  thread_id: string;
  tool_name: string;
  tier: "READ" | "LOW" | "HIGH";
  args: Record<string, unknown>;
  summary: string | null;
  status: ApprovalStatus;
  requested_by: string | null;
  decided_by: string | null;
  decision_note: string | null;
  created_at: string;
  decided_at: string | null;
  consumed_at: string | null;
  execution: Execution | null;
}

/** What `POST /approvals/{id}` answers with. */
export interface Decision {
  approval: Approval;
  resumed: boolean;
}

/** What one turn spent, as `GET /threads/{id}/turns` reports it. */
export interface RestoredUsage {
  input_tokens: number;
  output_tokens: number;
  /**
   * A string on the wire, not a number.
   *
   * The column is `NUMERIC` and Pydantic serialises `Decimal` as a string, so
   * no precision is lost on the way out — verified against the running
   * endpoint rather than assumed. Typed honestly here and converted once, in
   * `restore()`: declaring it `number` compiles and then throws at
   * `cost.toFixed(6)` with a blank turn where the price should be.
   *
   * Null for a model with no price on file, which is not the same statement as
   * zero. See `TurnUsageOut` in app/api/threads.py.
   */
  cost_usd: string | null;
  latency_ms: number | null;
}

/**
 * One turn of a thread, restored from the server.
 *
 * snake_case and hand-written for the same reason `Approval` is: these are
 * REST models, not the SSE contract. `citations` is the exception and a
 * deliberate one — it carries the contract's camelCase `Citation`, so a
 * restored turn and a streamed turn hold the same object by the time anything
 * renders them. The backend's `Turn` model says so from the other side.
 */
export interface RestoredTurn {
  question: string;
  answer: string | null;
  route: string | null;
  route_confidence: number | null;
  retrieved_count: number;
  citations: Citation[];
  action_outcome: string | null;
  approval: Approval | null;
  /** Nodes the turn is still paused at. Non-empty means its approval is live
   *  and can still be decided, rather than a decision already taken. */
  awaiting: string[];
  started_at: string | null;
  usage: RestoredUsage | null;
}

/** A thread, turn by turn. */
export interface ThreadTurns {
  thread_id: string;
  turns: RestoredTurn[];
  /** True when the thread was too long to walk and its oldest turns are
   *  missing. The newest are always present. */
  truncated: boolean;
}

/** One indexed document, as `GET /documents` returns it.
 *
 *  `chunk_count` is the number of embedded passages. Zero is the row worth
 *  looking at: the document is on file and the agent can never cite it. */
export interface IndexedDocument {
  id: string;
  title: string | null;
  chunk_count: number;
  page_count: number | null;
  version: number;
  ingested_at: string;
}
