import { describe, expect, it } from "vitest";
import { speakable } from "./speakable.js";

describe("speakable", () => {
  it("authors pauses: each line/list item/heading becomes its own sentence", () => {
    // The core fix — whitespace was collapsed BEFORE newlines could become pauses, so a
    // list ran together. Every line now ends in terminal punctuation.
    expect(speakable("## Setup steps\n- Preheat oven\n- Add flour\n- Bake")).toBe(
      "Setup steps. Preheat oven. Add flour. Bake.",
    );
    expect(speakable("First line\nSecond line")).toBe("First line. Second line.");
  });

  it("keeps an existing terminal mark rather than doubling it", () => {
    expect(speakable("Done already.\nNext one!")).toBe("Done already. Next one!");
  });

  it("linearizes a pipe table into one sentence per row", () => {
    const md = "| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |";
    expect(speakable(md)).toBe(
      "Name, Age. Row one: Name, Alice. Age, thirty. Row two: Name, Bob. Age, twenty five.",
    );
  });

  it("announces fenced code blocks instead of reading them", () => {
    expect(speakable("Try this:\n```py\nprint(1)\n```\nDone.")).toBe("Try this: code block. Done.");
  });

  it("keeps inline code and link text, drops the URL and images", () => {
    expect(speakable("Run `npm ci` then see [the docs](https://x.io/a/b).")).toBe(
      "Run npm ci then see the docs.",
    );
    expect(speakable("![a chart](c.png) shows growth.")).toBe("shows growth.");
  });

  it("reads a bare URL as its domain, not the slug", () => {
    expect(speakable("Visit https://github.com/OHF-Voice/piper1-gpl for more")).toBe(
      "Visit github dot com for more.",
    );
    expect(speakable("see www.example.co.uk/path here")).toBe("see example dot co dot uk here.");
  });

  it("verbalizes currency, percent, ranges and decimals", () => {
    expect(speakable("It cost $1,250.50 today.")).toBe(
      "It cost one thousand two hundred fifty dollars and fifty cents today.",
    );
    expect(speakable("Up 50% this week.")).toBe("Up fifty percent this week.");
    expect(speakable("Pick 3-5 items.")).toBe("Pick three to five items.");
    expect(speakable("Version 3.14 shipped.")).toBe("Version three point one four shipped.");
    expect(speakable("There are 1,024 files.")).toBe("There are one thousand twenty four files.");
  });

  it("verbalizes the allow-list emoji and drops the rest", () => {
    expect(speakable("Done ✅ and shipped 🚀")).toBe("Done check and shipped.");
    expect(speakable("Careful ⚠️ here")).toBe("Careful warning here.");
  });

  it("maps stray symbols to words", () => {
    expect(speakable("A & B need it")).toBe("A and B need it.");
    expect(speakable("go from a → b")).toBe("go from a to b.");
    expect(speakable("it is 20° outside")).toBe("it is twenty degrees outside.");
  });

  it("strips emphasis, headings, blockquotes and horizontal rules", () => {
    expect(speakable("**Bold** and _italic_ and ~~struck~~.")).toBe("Bold and italic and struck.");
    expect(speakable("> quoted note\n\n---\n\nafter rule")).toBe("quoted note. after rule.");
  });

  it("is a no-op-ish on plain prose and handles empty input", () => {
    expect(speakable("Just a normal sentence.")).toBe("Just a normal sentence.");
    expect(speakable("")).toBe("");
    // @ts-expect-error — defends against a null/undefined answer body
    expect(speakable(null)).toBe("");
  });
});
