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
