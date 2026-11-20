/**
 * The block layer: paragraphs, and tables.
 *
 * The fixture is a real portfolio answer, read out of a checkpoint in the
 * development database on 2026-11-20 rather than invented — a hand-written
 * table is a table the parser was written against, which is the one shape that
 * proves nothing. The rest are the ways a hand-written parser gets a table
 * wrong, and the one case where the right answer is to refuse.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Blocks } from "./markdown-blocks";

/** A real answer, prose then table, as the portfolio agent wrote it. */
const REAL_ANSWER = [
  "The following accounts hold MER011 along with the current position value in each:",
  "",
  "| Account ID | Market Value   |",
  "|------------|----------------|",
  "| AC-00001   | £99,668.83     |",
  "| AC-00002   | £621,122.24    |",
].join("\n");

describe("markdown blocks", () => {
  it("renders a real portfolio answer's table as a table", () => {
    render(<Blocks text={REAL_ANSWER} />);

    expect(screen.getByRole("columnheader", { name: "Account ID" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Market Value" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "AC-00002" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "£621,122.24" })).toBeInTheDocument();
    // Two body rows and a header row.
    expect(screen.getAllByRole("row")).toHaveLength(3);
  });

  it("leaves no pipes or dashes on the screen", () => {
    // The bug: every one of these was text an adviser had to read past.
    const { container } = render(<Blocks text={REAL_ANSWER} />);

    expect(container.textContent).not.toContain("|");
    expect(container.textContent).not.toContain("---");
  });

  it("keeps the sentence before the table as a sentence", () => {
    const { container } = render(<Blocks text={REAL_ANSWER} />);

    const paragraphs = container.querySelectorAll("p");
    expect(paragraphs).toHaveLength(1);
    expect(paragraphs[0].textContent).toBe(
      "The following accounts hold MER011 along with the current position value in each:",
    );
  });

  it("keeps prose after a table too, in order", () => {
    // `sql-segment-split-01` ends this way: the table, then a sentence about
    // it. A parser that stops at the table would drop the sentence.
    const { container } = render(
      <Blocks
        text={["| Segment | Value |", "|---|---|", "| hnw | £48m |", "", "Split by segment."].join(
          "\n",
        )}
      />,
    );

    expect(container.querySelector("table")).toBeInTheDocument();
    expect(container.querySelectorAll("p")).toHaveLength(1);
    expect(container.textContent).toContain("Split by segment.");
  });

  it("does not turn a sentence containing a pipe into a table", () => {
    // The reason a separator row is required. Without it this is a one-row
    // table whose heading is half a sentence.
    const { container } = render(<Blocks text="The mandate is discretionary | advisory." />);

    expect(container.querySelector("table")).not.toBeInTheDocument();
    expect(container.textContent).toBe("The mandate is discretionary | advisory.");
  });

  it("does not read two prose lines as a header and a row", () => {
    // The separator rule, pinned where it is the only thing doing the work:
    // with a line after it, the length guard no longer refuses this. Without
    // the rule, half of the first sentence becomes a column heading.
    const { container } = render(
      <Blocks
        text={["The mandate is discretionary | advisory.", "No account matched."].join("\n")}
      />,
    );

    expect(container.querySelector("table")).not.toBeInTheDocument();
    expect(container.textContent).toContain("discretionary | advisory.");
  });

  it("does not read two pipe-bearing sentences as a header and a row", () => {
    // What refuses these is the bounding pipe, not the separator: a row has
    // to start or end with one, and a sentence does neither. Pinned
    // separately from the separator rule below because they are two rules and
    // either alone would let a sentence become a column heading.
    const { container } = render(
      <Blocks
        text={["The regions are UK | North America.", "The values are £1.5m | £300k."].join("\n")}
      />,
    );

    expect(container.querySelector("table")).not.toBeInTheDocument();
    expect(container.textContent).toContain("The regions are UK | North America.");
  });

  it("does not read a bordered row as a header without a separator", () => {
    // The case that isolates the dashes rule: both lines are bounded by pipes
    // and the same width, so nothing else refuses them. GFM says two rows
    // with no separator are not a table, and the reason is honesty — nothing
    // said the first line was a heading. It is also the mid-stream shape, so
    // this agrees with the streaming case below.
    const { container } = render(
      <Blocks
        text={["| Account ID | Market Value |", "| AC-00001   | £99,668.83   |"].join("\n")}
      />,
    );

    expect(container.querySelector("table")).not.toBeInTheDocument();
    expect(container.textContent).toContain("£99,668.83");
  });

  it("shows a table with no outer pipes as text, which is the narrowing", () => {
    // GFM accepts this. We do not, and the choice is the point: all 8 tables
    // in the `sql` suite's answers are fully bounded, and dropping the
    // requirement makes every line of prose with a pipe a candidate heading.
    // If a model starts writing them unbounded, this test is what says so.
    const { container } = render(
      <Blocks text={["Region | Value", "---|---", "UK | £1.5m"].join("\n")} />,
    );

    expect(container.querySelector("table")).not.toBeInTheDocument();
    expect(container.textContent).toContain("Region | Value");
  });

  it("does not read a separator of the wrong width as one", () => {
    // Two lines that are not one table. Reading them as one would put
    // "Market Value" under a heading that was never written.
    const { container } = render(
      <Blocks text={["| Account ID | Market Value |", "|---|", "| AC-00001 | £99 |"].join("\n")} />,
    );

    expect(container.querySelector("table")).not.toBeInTheDocument();
  });

  it("shows a table that has not finished arriving as text", () => {
    // Mid-stream, before the separator row lands. It snaps into a table when
    // the second line arrives, and the `replace` at the end of the turn is
    // what makes the final text exact either way.
    const { container } = render(<Blocks text="| Account ID | Market Value |" />);

    expect(container.querySelector("table")).not.toBeInTheDocument();
    expect(container.textContent).toContain("| Account ID | Market Value |");
  });

  it("refuses the whole table when a row has more cells than the header", () => {
    // An unescaped pipe inside a cell. Dropping the extra cell would put
    // every value after it under the wrong heading, and these are amounts of
    // money — so the honest answer is to show the text and align nothing.
    const { container } = render(
      <Blocks
        text={[
          "| Instrument | Market Value |",
          "|---|---|",
          "| Northgate | Multi-Asset | £276,228.86 |",
        ].join("\n")}
      />,
    );

    expect(container.querySelector("table")).not.toBeInTheDocument();
    expect(container.textContent).toContain("£276,228.86");
  });

  it("pads a row that is short rather than shifting it", () => {
    render(
      <Blocks
        text={["| Ticker | Region | Value |", "|---|---|---|", "| MER011 | UK |"].join("\n")}
      />,
    );

    const cells = screen.getAllByRole("cell");
    expect(cells.map((cell) => cell.textContent)).toEqual(["MER011", "UK", ""]);
  });

  it("accepts the alignment colons a model writes in a separator", () => {
    render(<Blocks text={["| Weight |", "|------:|", "| 42.47 |"].join("\n")} />);

    expect(screen.getByRole("cell", { name: "42.47" })).toBeInTheDocument();
  });

  it("renders emphasis inside a cell through the inline parser", () => {
    const { container } = render(
      <Blocks text={["| Total |", "|---|", "| **£394,060.01** |"].join("\n")} />,
    );

    expect(container.querySelector("td strong")).toHaveTextContent("£394,060.01");
  });

  it("cannot turn a link or a script in a cell into either", () => {
    // The guarantee the whole file is built around, asserted at the layer that
    // was added: an answer is model output built from the corpus, which is
    // where an injection would put one.
    const { container } = render(
      <Blocks
        text={[
          "| Instrument |",
          "|---|",
          "| [click here](https://evil.test) |",
          "| <script>alert(1)</script> |",
        ].join("\n")}
      />,
    );

    expect(container.querySelector("a")).not.toBeInTheDocument();
    expect(container.querySelector("script")).not.toBeInTheDocument();
    expect(container.textContent).toContain("[click here](https://evil.test)");
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("renders an answer with no table exactly as it did before", () => {
    // The 75 `rag` answers are all this shape, and none of them must change.
    const { container } = render(<Blocks text="The minimum annual fee is **£1,500**." />);

    expect(container.querySelectorAll("p")).toHaveLength(1);
    expect(container.querySelector("p")).toHaveClass("whitespace-pre-wrap");
    expect(container.querySelector("strong")).toHaveTextContent("£1,500");
  });

  it("keeps a newline inside a paragraph, which pre-wrap shows", () => {
    const { container } = render(<Blocks text={"First line.\nSecond line."} />);

    expect(container.querySelectorAll("p")).toHaveLength(1);
    expect(container.querySelector("p")?.textContent).toBe("First line.\nSecond line.");
  });

  it("renders nothing for an empty answer rather than an empty paragraph", () => {
    const { container } = render(<Blocks text="   " />);

    expect(container.querySelector("p")).not.toBeInTheDocument();
  });
});
