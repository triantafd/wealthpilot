/**
 * Typed fetch wrapper.
 *
 * Requests go to relative `/api/...` paths, which Vite proxies to the backend
 * on port 8000 in development (see vite.config.ts). That keeps the base URL out
 * of the code entirely, so there is no environment variable to get wrong.
 *
 * The SSE client for streaming agent events is separate and arrives with the
 * graph in Phase 4; this is for ordinary JSON endpoints.
 */

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** GET a JSON endpoint. Throws ApiError on a non-2xx response. */
export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { Accept: "application/json" },
    signal,
  });

  if (!response.ok) {
    throw new ApiError(response.status, `GET ${path} failed with ${response.status}`);
  }

  return (await response.json()) as T;
}

/** Shape of `GET /health`, mirroring the Health model in app/main.py. */
export interface Health {
  status: "ok";
  version: string;
  env: string;
}
