/**
 * The inline markdown renderer.
 *
 * Mostly these are the cases a hand-written tokenizer gets wrong, because
 * that is the honest risk of not using a library: a sum read as emphasis, a
 * snake_case identifier read as emphasis, and markup rendered as markup.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Inline } from "./inline-markdown";

/** The rendered text, as a reader sees it. */
function shown(text: string): string {
  const { container } = render(<Inline text={text} />);
  return container.textContent ?? "";
}

describe("inline markdown", () => {
  it("renders bold as bold, without the asterisks", () => {
    const { container } = render(<Inline text="The minimum fee is **£1,500**." />);

    expect(container.querySelector("strong")).toHaveTextContent("£1,500");
    expect(container.textContent).toBe("The minimum fee is £1,500.");
  });

  it("renders a single-asterisk italic, which document names arrive in", () => {
    const { container } = render(<Inline text="See *Schedule of Fees and Charges*." />);

    expect(container.querySelector("em")).toHaveTextContent("Schedule of Fees and Charges");
    expect(container.textContent).toBe("See Schedule of Fees and Charges.");
  });

  it("renders a code span and leaves its contents alone", () => {
    const { container } = render(<Inline text="The mandate is `execution_only`." />);

    expect(container.querySelector("code")).toHaveTextContent("execution_only");
  });

  it("does not parse markdown inside a code span", () => {
    expect(shown("`**not bold**`")).toBe("**not bold**");
  });

  it("leaves arithmetic alone", () => {
    // The failure this renderer exists to avoid: a fee calculation silently
    // becoming italics with its asterisks eaten.
    const sum = "£1,400,000 * 0.0085 * 12 months";
    const { container } = render(<Inline text={sum} />);

    expect(container.querySelector("em")).toBeNull();
    expect(container.textContent).toBe(sum);
  });

  it("leaves snake_case identifiers alone", () => {
    // Why underscore emphasis is not supported at all: these two would
    // otherwise open and close a span across the words between them.
    const text = "execution_only accounts differ from discretionary_managed ones";

    const { container } = render(<Inline text={text} />);

    expect(container.querySelector("em")).toBeNull();
    expect(container.textContent).toBe(text);
  });

  it("leaves an unclosed delimiter as text, which is what streaming looks like", () => {
    expect(shown("the fee is **£1,5")).toBe("the fee is **£1,5");
    expect(shown("a lone * asterisk")).toBe("a lone * asterisk");
  });

  it("nests emphasis inside bold", () => {
    const { container } = render(<Inline text="**£1,500 *per account***" />);

    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong?.querySelector("em")).toHaveTextContent("per account");
    expect(container.textContent).toBe("£1,500 per account");
  });

  it("renders markup as text, not as markup", () => {
    // An answer is model output built from the corpus — the one place a prompt
    // injection would try to put a link, an image or a script.
    const { container } = render(
      <Inline text='<script>alert(1)</script> [click](javascript:alert(1)) ![](http://x/y.png)' />,
    );

    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/script>alert\(1\)/)).toBeInTheDocument();
    expect(container.textContent).toContain("[click](javascript:alert(1))");
  });

  it("passes plain text through unchanged", () => {
    const plain = "An execution-only account must never be rebalanced by the firm.";

    expect(shown(plain)).toBe(plain);
  });
});
