import { describe, expect, it } from "vitest";
import { chunkStream, speakable } from "./speakable.js";

/** Feed `full` to chunkStream in `steps` growing prefixes (simulating streaming), advancing
 * a raw cursor, then flush — returning every clip emitted in order. */
function drive(full: string, steps: number): string[] {
  const all: string[] = [];
  let cursor = 0;
  for (let k = 1; k <= steps; k++) {
    const prefix = full.slice(0, Math.ceil((full.length * k) / steps));
    const { chunks, consumed } = chunkStream(prefix.slice(cursor), false);
    all.push(...chunks);
    cursor += consumed;
  }
  const { chunks } = chunkStream(full.slice(cursor), true);
  all.push(...chunks);
  return all;
}

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
      "Name, Age. Row one: Name, Alice; Age, thirty. Row two: Name, Bob; Age, twenty five.",
    );
  });

  it("does not read a ragged table row's extra cells as nothing, and ignores a bare rule", () => {
    // A body row wider than the header keeps the extra cell (no silent drop).
    expect(speakable("| A | B |\n|---|---|\n| 1 | 2 | 3 |")).toBe(
      "A, B. Row one: A, one; B, two; three.",
    );
    // A prose line with a pipe followed by a horizontal rule is NOT a table.
    expect(speakable("apples | oranges\n-----\nDone.")).toBe("apples oranges. Done.");
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

  it("brackets a parenthetical with commas so piper pauses around it", () => {
    expect(speakable("defence spending (target 5% by 2035) and reaffirm")).toBe(
      "defence spending, target five percent by two thousand thirty five, and reaffirm.",
    );
    // A parenthetical at a sentence end folds into the period — no ",." / ".," stutter.
    expect(speakable("raise it (a lot).")).toBe("raise it, a lot.");
    expect(speakable("He agreed (reluctantly.)")).toBe("He agreed, reluctantly.");
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

describe("chunkStream", () => {
  it("emits one clip per complete sentence and holds a partial tail", () => {
    expect(chunkStream("One sentence. Two sentence. Thr", false)).toEqual({
      chunks: ["One sentence.", "Two sentence."],
      consumed: "One sentence. Two sentence. ".length,
    });
  });

  it("holds an open code fence until it closes", () => {
    const open = "Here:\n```py\nprint(1)\n";
    expect(chunkStream(open, false).chunks).toEqual(["Here:"]); // fence body held
    const closed = `${open}print(2)\n\`\`\`\nDone.`;
    expect(chunkStream(closed, true).chunks).toEqual(["Here: code block.", "Done."]);
  });

  it("holds a streaming table until the block is complete, then linearizes it whole", () => {
    const partial = "Scores:\n| Name | Pts |\n|------|-----|\n| Al | 3 |\n";
    // The table body is still open (a next row could arrive), so only the lead-in commits.
    expect(chunkStream(partial, false).chunks).toEqual(["Scores:"]);
    const full = `${partial}| Bo | 2 |\n\nAfter.`;
    const clips = chunkStream(full, true).chunks;
    expect(clips[0]).toContain("Scores:"); // label + column lead-in merge (label ends in ":")
    expect(clips).toContain("Row one: Name, Al; Pts, three.");
    expect(clips).toContain("Row two: Name, Bo; Pts, two.");
    expect(clips.at(-1)).toBe("After.");
  });

  it("streaming in any number of steps yields the same clips as one shot (no loss/dup)", () => {
    const doc =
      "# Report\n\nWe shipped 3 things today.\n\n- Fixed login\n- Added 2 charts\n\n" +
      "| Item | Qty |\n|------|-----|\n| Milk | 2 |\n| Eggs | 12 |\n\n" +
      "```js\nconst x = 1;\n```\n\nSee https://example.com/docs for 50% more. Done ✅";
    const oneShot = chunkStream(doc, true).chunks;
    for (const steps of [1, 2, 3, 7, 20, doc.length]) {
      expect(drive(doc, steps)).toEqual(oneShot);
    }
  });
});
