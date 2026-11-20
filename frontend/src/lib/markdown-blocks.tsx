/**
 * The block structure of an answer: paragraphs, and tables.
 *
 * `inline-markdown.tsx` handles what happens *inside* a line. This handles
 * what the lines are, which until now was nothing — an answer was one
 * paragraph with `whitespace-pre-wrap` under it.
 *
 * **Why tables and nothing else.** `portfolio_answer.md` asks for a short
 * markdown table wherever several rows are the answer, because that is what an
 * allocation is. Measured 2026-11-20 on the 51 cases of the `sql` suite, 8
 * answers contain table syntax; on the 75 of `rag`, none do, and none contain
 * a newline either. So one construct has arrived and the other thirty have
 * not.
 *
 * **Why not `react-markdown`.** It does not render raw HTML, but it *does*
 * render links and images, and an answer is model output built from the
 * corpus — exactly where a prompt injection would put one. It can be told not
 * to, with `disallowedElements`, and that is the difference: the guarantee
 * would become "configured not to render links" rather than "cannot render
 * one". Here no URL is ever parsed and nothing but React elements is ever
 * produced, so a link, an image or a `<script>` in an answer is text on the
 * screen by construction. If answers later grow lists, headings and block
 * quotes, that argument loses to the cost of hand-writing a parser one
 * construct at a time, and `react-markdown` with an allow-list is the right
 * call. Recorded in `docs/NOTES.md` with that trigger.
 *
 * **A table needs its separator row.** Without that requirement any sentence
 * containing a pipe becomes a table. It also means a table arriving over the
 * stream renders as text until its second line lands, and then snaps into
 * shape — the `replace` the backend sends at the end of a turn is what makes
 * the final text exact either way.
 */

import type { ReactNode } from "react";

import { Inline } from "./inline-markdown";

/**
 * A row: bounded by a pipe at one end at least.
 *
 * Narrower than GFM, which accepts `Region | Value` with no outer pipes. A
 * deliberate narrowing: all 8 tables in the `sql` suite's answers and the one
 * read out of a real checkpoint are fully bounded, and without the
 * requirement every line of prose containing a pipe becomes a candidate
 * heading. An unbounded table therefore shows as text — the old behaviour,
 * and the right failure for a parser this small.
 */
function looksLikeRow(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.includes("|") && (trimmed.startsWith("|") || trimmed.endsWith("|"));
}

/** `|---|---:|`, the line that makes the one above it a header. */
function isSeparator(line: string): boolean {
  const cells = splitCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{1,}:?$/.test(cell));
}

/** The cells of a row, without the pipes that bound it. */
function splitCells(line: string): string[] {
  let trimmed = line.trim();
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
  return trimmed.split("|").map((cell) => cell.trim());
}

interface Table {
  kind: "table";
  header: string[];
  rows: string[][];
}

interface Paragraph {
  kind: "paragraph";
  text: string;
}

type Block = Table | Paragraph;

/**
 * A table starting at `start`, or null if those lines are not one.
 *
 * Refused when a body row has **more** cells than the header, which is what an
 * unescaped pipe inside a cell looks like. Padding a short row with blanks is
 * safe; silently dropping the extra cell of a long one is not, because the
 * values after it would then sit under the wrong heading — and these are
 * account numbers and amounts of money. A refused table falls through to text,
 * which is what the whole answer looked like yesterday.
 */
function tableAt(lines: string[], start: number): { table: Table; end: number } | null {
  if (!looksLikeRow(lines[start]) || start + 1 >= lines.length) return null;
  if (!isSeparator(lines[start + 1])) return null;

  const header = splitCells(lines[start]);
  // GFM's rule, and worth keeping for the reason it exists: a separator of a
  // different width means the two lines were not written as one table, so
  // reading them as one would invent a heading.
  if (splitCells(lines[start + 1]).length !== header.length) return null;
  const rows: string[][] = [];
  let at = start + 2;

  while (at < lines.length && looksLikeRow(lines[at]) && !isSeparator(lines[at])) {
    const cells = splitCells(lines[at]);
    if (cells.length > header.length) return null;
    rows.push([...cells, ...Array(header.length - cells.length).fill("")]);
    at += 1;
  }

  return { table: { kind: "table", header, rows }, end: at };
}

/** The blocks of an answer, in order. */
function toBlocks(text: string): Block[] {
  const lines = text.split("\n");
  const blocks: Block[] = [];
  let prose: string[] = [];

  const flush = () => {
    // Blank lines around a table belong to the separation, not to the prose:
    // keeping them would leave an empty paragraph with its own margin.
    const joined = prose.join("\n").replace(/^\n+|\n+$/g, "");
    if (joined.trim()) blocks.push({ kind: "paragraph", text: joined });
    prose = [];
  };

  for (let index = 0; index < lines.length; ) {
    const found = tableAt(lines, index);
    if (found) {
      flush();
      blocks.push(found.table);
      index = found.end;
      continue;
    }
    prose.push(lines[index]);
    index += 1;
  }

  flush();
  return blocks;
}

function TableBlock({ table }: { table: Table }) {
  return (
    // `overflow-x-auto`, because a table of every column of `v_account_summary`
    // is wider than a chat bubble and the alternative is a page that scrolls
    // sideways.
    <div className="overflow-x-auto">
      {/* `w-auto`, not `w-full`: a two-column table stretched across the
            bubble puts the figure a hand's width from the account it belongs
            to. Content width reads better, and the scroller above handles the
            17-column case. */}
        <table className="w-auto border-collapse text-xs">
        <thead>
          <tr className="border-b">
            {table.header.map((cell, column) => (
              <th key={column} className="px-2 py-1 text-left font-medium whitespace-nowrap">
                <Inline text={cell} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, index) => (
            <tr key={index} className="border-b last:border-0">
              {row.map((cell, column) => (
                <td key={column} className="px-2 py-1 whitespace-nowrap">
                  <Inline text={cell} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * An answer, as blocks.
 *
 * A `div` rather than a `p`, which is what the call site used to be: a
 * `table` inside a `p` is invalid HTML and the browser closes the paragraph
 * before it, so the layout breaks in a way React does not warn about.
 * `whitespace-pre-wrap` moves onto the paragraphs, where it belongs — on the
 * wrapper it would make the padding inside every cell visible.
 */
export function Blocks({ text }: { text: string }) {
  const blocks = toBlocks(text);

  return (
    <>
      {blocks.map((block, index): ReactNode =>
        block.kind === "table" ? (
          <TableBlock key={index} table={block} />
        ) : (
          <p key={index} className="whitespace-pre-wrap">
            <Inline text={block.text} />
          </p>
        ),
      )}
    </>
  );
}
