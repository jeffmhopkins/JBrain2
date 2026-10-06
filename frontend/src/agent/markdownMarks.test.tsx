// Inline tool marks inside the answer's markdown: a mark must never break what it sits in.
// The offset is moved onto a safe boundary first (`snapBreak`), and the mark rides a sentinel
// the inline renderer turns into the node, so the prose parses as it would without it.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown, placeMarks, snapBreak } from "./markdown";

const at = (text: string, needle: string): number => text.indexOf(needle) + needle.length;

describe("snapBreak", () => {
  it("keeps an offset at the end of a paragraph, inline", () => {
    const text = "Let me look.\n\nThe page says so.";
    expect(snapBreak(text, at(text, "look."))).toEqual({ at: at(text, "look."), own: false });
  });

  it("moves an offset between blocks, or at a block's start, to the end of the block before", () => {
    const text = "Let me look.\n\nThe page says so.";
    const end = at(text, "look.");
    expect(snapBreak(text, end + 1)).toEqual({ at: end, own: false });
    expect(snapBreak(text, end + 2)).toEqual({ at: end, own: false });
  });

  it("puts an offset at the very start nowhere special", () => {
    expect(snapBreak("\n\nHello", 1)).toEqual({ at: 1, own: true });
  });

  it("moves an offset inside a table to after it, on a line of its own", () => {
    const text =
      "Times:\n\n| Film | Time |\n| --- | --- |\n| Dune | 7:15 |\n| Elio | 9:00 |\n\nDone.";
    const end = at(text, "| Elio | 9:00 |");
    expect(snapBreak(text, at(text, "| Dune"))).toEqual({ at: end, own: true });
  });

  it("moves an offset inside a code fence to after its closing fence", () => {
    const text = "Run:\n\n```\nprint(1)\nprint(2)\n```\nAfter.";
    expect(snapBreak(text, at(text, "print(1)"))).toEqual({
      at: at(text, "```\nAfter") - 6,
      own: true,
    });
  });

  it("moves an offset inside a list to the end of its last item, inline", () => {
    const text = "Picks:\n\n- one\n- two\n\n- three\n\nNext.";
    expect(snapBreak(text, at(text, "- one"))).toEqual({ at: at(text, "- three"), own: false });
  });

  it("steps out of an inline token the offset would cut", () => {
    const text = "It is **very good** news.";
    expect(snapBreak(text, at(text, "**ve"))).toEqual({ at: at(text, "good**"), own: false });
    const link = "See [the page](https://x.example/a) now.";
    expect(snapBreak(link, at(link, "[the"))).toEqual({ at: at(link, "/a)"), own: false });
  });

  it("clamps an offset past the end", () => {
    expect(snapBreak("Hi.", 99)).toEqual({ at: 3, own: false });
  });
});

describe("placeMarks", () => {
  it("inserts one sentinel per offset, in order, at safe boundaries", () => {
    const text = "One.\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\nTwo.";
    const out = placeMarks(text, [at(text, "One."), at(text, "| 1"), at(text, "One.")]);
    expect(out).toBe("One.\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n\n\n\n\nTwo.");
  });

  it("neutralises a sentinel-range character the answer already held", () => {
    expect(placeMarks("ab.", [])).toBe("a�b.");
  });
});

describe("Markdown with marks", () => {
  it("renders a mark inline at the end of its paragraph", () => {
    const text = "Let me look.\n\nThe page says so.";
    const { container } = render(
      <Markdown text={text} marks={[{ at: at(text, "look."), node: <b className="m">M</b> }]} />,
    );
    const paragraphs = container.querySelectorAll("p.md-p");
    expect(paragraphs).toHaveLength(2);
    expect(paragraphs[0]?.querySelector(".m")).not.toBeNull();
    expect(paragraphs[0]?.textContent).toBe("Let me look.M");
  });

  it("never breaks a table, fence or bold run it would have cut", () => {
    const text =
      "It is **very good**.\n\n| Film | Time |\n| --- | --- |\n| Dune | 7:15 |\n\n```\ncode\n```\nEnd.";
    const { container } = render(
      <Markdown
        text={text}
        marks={[
          { at: at(text, "**ve"), node: <i className="m">1</i> },
          { at: at(text, "| Du"), node: <i className="m">2</i> },
          { at: at(text, "co"), node: <i className="m">3</i> },
        ]}
      />,
    );
    expect(container.querySelector("strong")?.textContent).toBe("very good");
    expect(container.querySelectorAll("table tbody tr")).toHaveLength(1);
    expect(container.querySelector("table .m")).toBeNull();
    expect(container.querySelector("pre code")?.textContent).toBe("code");
    expect([...container.querySelectorAll(".m")].map((m) => m.textContent)).toEqual([
      "1",
      "2",
      "3",
    ]);
  });

  it("puts a mark inside a list at the end of its last item", () => {
    const text = "- one\n- two\n\nAfter.";
    const { container } = render(
      <Markdown text={text} marks={[{ at: 3, node: <i className="m">x</i> }]} />,
    );
    const items = container.querySelectorAll("li");
    expect(items[1]?.textContent).toBe("twox");
  });

  it("renders text with no marks exactly as before", () => {
    const { container: a } = render(<Markdown text={"Hi **there**.\n\n- a"} />);
    const { container: b } = render(<Markdown text={"Hi **there**.\n\n- a"} marks={[]} />);
    expect(a.innerHTML).toBe(b.innerHTML);
  });
});
