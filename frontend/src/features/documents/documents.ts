/**
 * The indexed corpus, for the documents page.
 *
 * Separate from the component because a hook exported from a component file
 * breaks React fast refresh — the same reason `turn.ts` and `history.ts` are
 * separate.
 */

import { useQuery } from "@tanstack/react-query";

import { apiGet, type IndexedDocument } from "@/lib/api";

export type { IndexedDocument } from "@/lib/api";

/** Everything in the corpus. Changes only when someone runs the ingest
 *  command, so this is as static as server state gets here. */
export function useDocuments() {
  return useQuery({
    queryKey: ["documents"],
    queryFn: ({ signal }) => apiGet<IndexedDocument[]>("/documents", signal),
  });
}

/**
 * An ingestion timestamp, as a date a reader can hold on to.
 *
 * Fixed locale and an explicit UTC zone rather than the browser's: the
 * timestamps are UTC, "09/10/2026" means two different days depending on who
 * is reading it, and a date that shifts with the viewer's clock is not
 * something to compare against a document on disk.
 */
export function ingestedOn(timestamp: string): string {
  const at = new Date(timestamp);
  if (Number.isNaN(at.getTime())) {
    // Rather than "Invalid Date" in the middle of the table.
    return "unknown";
  }
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(at);
}
