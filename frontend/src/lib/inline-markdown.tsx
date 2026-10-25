/**
 * The small part of markdown that answers actually contain.
 *
 * The model writes one direct paragraph — the prompt asks for exactly that —
 * and emphasises figures inside it. Across the 75 answers in the last full
 * `rag` eval report: `**bold**` in 19, `*italic*` in 11, `` `code` `` in 4,
 * and **no** bullets, numbered lists, headings, tables or newlines at all.
 * So this is an inline-formatting problem, and `react-markdown` would be a
 * remark/micromark pipeline brought in to render three constructs.
 *
 * It also renders untrusted text. An answer is model output built from the
 * corpus, which is exactly where a prompt injection would put a link or an
 * image. This returns React elements and nothing else — no
 * `dangerouslySetInnerHTML`, no URL ever parsed — so a link, an image, or a
 * `<script>` in an answer is text on the screen and cannot be anything else.
 * That is the security property, and it holds by construction rather than by
 * an allow-list somebody has to maintain.
 *
 * **Underscore emphasis is deliberately not supported.** `_x_` appeared in
 * none of those 75 answers, while snake_case identifiers — `execution_only`,
 * `ask_account`, `account_type` — are everywhere in the corpus, so treating
 * `_` as a delimiter would italicise the gap between two of them. Underscores
 * are literal text here.
 *
 * What this does not do: lists, tables, headings, links, images, raw HTML,
 * block quotes. If answers ever grow them, they will show literally and this
 * becomes a reason to adopt a real parser rather than to extend this file.
 */

import type { ReactNode } from "react";

/** Markdown's flanking rule, which is the whole reason this is not a regex.
 *
 *  An opening delimiter must be followed by non-whitespace and a closing one
 *  preceded by it. Without this, `£1,400,000 * 0.0085 * 12` is a sum that
 *  renders as italics — in an app about money, with the asterisks eaten. */
function isSpace(char: string | undefined): boolean {
  return char === undefined || /\s/.test(char);
}

/** Where `marker` closes the run that opened at `from`, or -1. */
function closingIndex(text: string, from: number, marker: string): number {
  if (isSpace(text[from])) return -1; // not an opener: `* ` is a bullet or a sum

  let at = text.indexOf(marker, from + 1);
  while (at !== -1) {
    // A single `*` whose neighbour is another `*` belongs to a `**` run, so it
    // cannot close an italic.
    const partOfStrongRun = marker === "*" && (text[at + 1] === "*" || text[at - 1] === "*");
    if (!partOfStrongRun && !isSpace(text[at - 1])) {
      // `**bold *and italic***` ends in a run of three. The inner italic takes
      // the first asterisk and the bold the last two, so a `**` closer is
      // aligned to the end of the run rather than to its start — otherwise
      // bold swallows the italic's delimiter and the italic never closes.
      let runEnd = at;
      while (text[runEnd] === "*") runEnd += 1;
      return marker === "**" ? Math.max(at, runEnd - 2) : at;
    }
    at = text.indexOf(marker, at + 1);
  }
  return -1;
}

/**
 * Render inline markdown as React nodes.
 *
 * Recursive, so `**£1,500 *per account***` nests correctly; recursion is on a
 * strictly shorter slice, so it terminates. An unmatched or badly placed
 * delimiter is left as the character it is, which is also what a half-arrived
 * streamed answer looks like for one chunk: `the fee is **£1,5`.
 *
 * Not exported: a file that exports a component and a function breaks React
 * fast refresh. `Inline` is the whole public surface, and the tests drive the
 * parser through it.
 */
function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let literal = "";
  let index = 0;

  const flush = () => {
    if (literal) {
      nodes.push(literal);
      literal = "";
    }
  };

  while (index < text.length) {
    const char = text[index];

    if (char === "`") {
      const end = text.indexOf("`", index + 1);
      if (end > index + 1) {
        flush();
        // Not parsed further: the point of a code span is that its content is
        // the characters it contains.
        nodes.push(
          <code key={nodes.length} className="rounded bg-muted px-1 py-0.5 text-[0.9em]">
            {text.slice(index + 1, end)}
          </code>,
        );
        index = end + 1;
        continue;
      }
    }

    if (char === "*") {
      const marker = text.startsWith("**", index) ? "**" : "*";
      const end = closingIndex(text, index + marker.length, marker);
      if (end !== -1) {
        flush();
        const inner = renderInline(text.slice(index + marker.length, end));
        nodes.push(
          marker === "**" ? (
            <strong key={nodes.length} className="font-semibold">
              {inner}
            </strong>
          ) : (
            <em key={nodes.length}>{inner}</em>
          ),
        );
        index = end + marker.length;
        continue;
      }
    }

    literal += char;
    index += 1;
  }

  flush();
  return nodes;
}

/** The parser as an element, which is how every call site uses it. */
export function Inline({ text }: { text: string }) {
  return <>{renderInline(text)}</>;
}
