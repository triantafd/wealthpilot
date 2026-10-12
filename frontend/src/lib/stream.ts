/**
 * Reading the agent's SSE stream.
 *
 * **Not `EventSource`.** That only issues GET requests, and
 * `POST /threads/{id}/messages` carries the message in a JSON body. So this
 * reads the response body itself and splits frames, which also means it can
 * surface a non-2xx status — `EventSource` reports a failed connection with no
 * status at all.
 *
 * Types come from `@shared/stream-events`, the same file the backend's contract
 * test compares against. Importing them here is the point of that file existing:
 * if the backend adds a field, this stops compiling.
 */

import { isStreamEvent, type StreamEvent } from "@shared/stream-events";

/** A frame is `data: <json>` and frames are separated by a blank line. */
const FRAME_SEPARATOR = "\n\n";
const DATA_PREFIX = "data: ";

export class StreamError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "StreamError";
    this.status = status;
  }
}

/** Split a buffer into complete frames, returning the unparsed remainder. */
export function takeFrames(buffer: string): { events: StreamEvent[]; rest: string } {
  const parts = buffer.split(FRAME_SEPARATOR);
  // The last part is whatever arrived after the final separator: either empty,
  // or half a frame still in flight. Keeping it is what stops a long answer
  // being torn in two by a chunk boundary.
  const rest = parts.pop() ?? "";

  const events: StreamEvent[] = [];
  for (const part of parts) {
    const line = part.trim();
    if (!line.startsWith(DATA_PREFIX)) continue;
    const parsed: unknown = JSON.parse(line.slice(DATA_PREFIX.length));
    if (isStreamEvent(parsed)) events.push(parsed);
  }
  return { events, rest };
}

export interface AskOptions {
  threadId: string;
  message: string;
  userId?: string;
  signal?: AbortSignal;
}

/**
 * Send a message and yield each event as it arrives.
 *
 * An async generator rather than a callback: the caller drives it with
 * `for await`, so React state updates happen in the component's own flow and
 * there is no subscription to tear down beyond the abort signal.
 */
export async function* ask(options: AskOptions): AsyncGenerator<StreamEvent> {
  const response = await fetch(`/api/threads/${options.threadId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ message: options.message, user_id: options.userId }),
    signal: options.signal,
  });

  if (!response.ok || !response.body) {
    // Validation failures arrive here rather than as an `error` event: the
    // server can only switch to a stream once it has accepted the request.
    throw new StreamError(response.status, `the server rejected the message (${response.status})`);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += value;
      const { events, rest } = takeFrames(buffer);
      buffer = rest;
      yield* events;
    }
    // A stream that ended mid-frame leaves a remainder; it is incomplete JSON
    // by definition, so there is nothing to recover.
  } finally {
    reader.releaseLock();
  }
}

/** Start a conversation, returning its id. */
export async function createThread(signal?: AbortSignal): Promise<string> {
  const response = await fetch("/api/threads", { method: "POST", signal });
  if (!response.ok) {
    throw new StreamError(response.status, `could not start a conversation (${response.status})`);
  }
  const body = (await response.json()) as { thread_id: string };
  return body.thread_id;
}

/** Decide an approval. Returns whether the graph resumed. */
export async function decideApproval(
  approvalId: string,
  decision: "approve" | "reject",
  options: { note?: string; decidedBy: string; signal?: AbortSignal },
): Promise<boolean> {
  const response = await fetch(`/api/approvals/${approvalId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      decision,
      note: options.note,
      // TEMPORARY, mirroring the backend: this must come from the
      // authenticated user before deployment. See ROADMAP Phase 9.
      decided_by: options.decidedBy,
    }),
    signal: options.signal,
  });
  if (!response.ok) {
    throw new StreamError(response.status, `the decision was not accepted (${response.status})`);
  }
  const body = (await response.json()) as { resumed: boolean };
  return body.resumed;
}
