/**
 * Splitting an SSE body into frames.
 *
 * The part most likely to break on a slow connection: a chunk boundary can
 * fall anywhere, including inside a frame's JSON.
 */

import { describe, expect, it } from "vitest";

import { takeFrames } from "./stream";

describe("takeFrames", () => {
  it("reads complete frames", () => {
    const buffer =
      'data: {"type":"route","route":"research","confidence":0.9}\n\n' +
      'data: {"type":"token","text":"hello"}\n\n';

    const { events, rest } = takeFrames(buffer);

    expect(events.map((e) => e.type)).toEqual(["route", "token"]);
    expect(rest).toBe("");
  });

  it("keeps a half-arrived frame for the next chunk", () => {
    // The failure this prevents: a long answer torn in two by a chunk
    // boundary, parsed as invalid JSON and thrown away.
    const first = 'data: {"type":"token","text":"The minimum annual';
    const second = ' advisory fee is £1,500."}\n\n';

    const one = takeFrames(first);
    expect(one.events).toHaveLength(0);
    expect(one.rest).toBe(first);

    const two = takeFrames(one.rest + second);
    expect(two.events).toHaveLength(1);
    expect(two.events[0]).toMatchObject({ type: "token" });
  });

  it("ignores anything that is not a data frame", () => {
    // Comments and keep-alives are legal SSE and must not become events.
    const { events } = takeFrames(': keep-alive\n\ndata: {"type":"token","text":"x"}\n\n');

    expect(events).toHaveLength(1);
  });

  it("drops a frame with no discriminator rather than yielding a broken event", () => {
    const { events } = takeFrames('data: {"nope":1}\n\n');

    expect(events).toHaveLength(0);
  });

  it("handles an empty buffer", () => {
    expect(takeFrames("")).toEqual({ events: [], rest: "" });
  });
});
