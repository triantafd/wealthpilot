/**
 * Formatting an ingestion timestamp.
 *
 * Pure, and worth its own test because the failure is silent: a date that
 * reads correctly to whoever wrote it and names the day before to a reader
 * five time zones away.
 */

import { describe, expect, it } from "vitest";

import { ingestedOn } from "./documents";

describe("ingestedOn", () => {
  it("formats a timestamp as a UTC date", () => {
    expect(ingestedOn("2026-10-09T19:12:00Z")).toBe("9 Oct 2026");
  });

  it("does not let the viewer's clock move the day", () => {
    // 23:30 UTC is already tomorrow east of here and still today in London.
    // The document was ingested on the 9th either way.
    expect(ingestedOn("2026-10-09T23:30:00Z")).toBe("9 Oct 2026");
    expect(ingestedOn("2026-10-09T00:30:00Z")).toBe("9 Oct 2026");
  });

  it("says so rather than printing Invalid Date", () => {
    expect(ingestedOn("not a timestamp")).toBe("unknown");
  });
});
