import { PageHeader } from "@/components/page-shell";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

import { type IndexedDocument, ingestedOn, useDocuments } from "./documents";

/**
 * The corpus the answers are grounded in.
 *
 * Read-only, and that is a decision rather than a gap: ingestion rewrites
 * what every citation points at, so it is a command and not a button. This
 * page answers the question an adviser actually has when an answer cites
 * something — is that document indexed, and how current is it?
 *
 * The row worth building the page for is a document with **no passages**. It
 * is on file and the agent can never cite it, and before this the only way to
 * find that out was psql. So it is called out in the table rather than left
 * as a quiet `0`.
 */
export function DocumentsPage() {
  const documents = useDocuments();

  return (
    <>
      <PageHeader title="Documents" description="The indexed policy corpus the agent cites" />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl px-4 py-6">
          {documents.isPending ? (
            <Loading />
          ) : documents.isError ? (
            <Failed message={documents.error.message} />
          ) : documents.data.length === 0 ? (
            <Empty />
          ) : (
            <Corpus documents={documents.data} />
          )}
        </div>
      </div>
    </>
  );
}

function Corpus({ documents }: { documents: IndexedDocument[] }) {
  const passages = documents.reduce((total, document) => total + document.chunk_count, 0);
  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Document</TableHead>
            <TableHead className="text-right">Passages</TableHead>
            <TableHead className="text-right">Pages</TableHead>
            <TableHead className="text-right">Last ingested</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {documents.map((document) => (
            <TableRow key={document.id} data-testid="document">
              <TableCell>
                <span className="font-medium">{document.title ?? document.id}</span>
                {/* The slug, because this is what a citation prints: an
                    adviser matching an answer's source to this list is
                    matching on the id, not on the title. */}
                <span className="block font-mono text-xs text-muted-foreground">
                  {document.id}
                </span>
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {document.chunk_count > 0 ? (
                  document.chunk_count
                ) : (
                  <span className="text-destructive" data-testid="not-searchable">
                    0 · not searchable
                  </span>
                )}
              </TableCell>
              <TableCell className="text-right tabular-nums text-muted-foreground">
                {document.page_count ?? "—"}
              </TableCell>
              <TableCell className="text-right text-muted-foreground">
                {ingestedOn(document.ingested_at)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <p className="mt-3 text-xs text-muted-foreground" data-testid="corpus-total">
        {documents.length} {documents.length === 1 ? "document" : "documents"} · {passages}{" "}
        passages indexed
      </p>
    </>
  );
}

function Loading() {
  return (
    <div className="space-y-2" data-testid="documents-loading">
      {[0, 1, 2, 3].map((row) => (
        <Skeleton key={row} className="h-10 w-full" />
      ))}
    </div>
  );
}

function Failed({ message }: { message: string }) {
  return (
    <div className="pt-10 text-center text-sm" data-testid="documents-failed">
      <p className="text-destructive">The corpus could not be loaded.</p>
      <p className="mt-1 text-xs text-muted-foreground">{message}</p>
    </div>
  );
}

/** Nothing ingested. Named as a command to run, because that is what it is:
 *  an empty corpus is not an error, it is a step nobody has taken yet. */
function Empty() {
  return (
    <div className="pt-10 text-center text-sm text-muted-foreground" data-testid="documents-empty">
      <p>Nothing is indexed yet, so every answer will refuse for want of a source.</p>
      <p className="mt-2 text-xs">
        Ingest the corpus with{" "}
        <code className="rounded bg-muted px-1 py-0.5 font-mono">
          uv run python -m app.scripts.ingest
        </code>
      </p>
    </div>
  );
}
