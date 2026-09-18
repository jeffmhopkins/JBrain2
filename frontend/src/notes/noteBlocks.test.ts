import { describe, expect, it } from "vitest";
import { parseNote, previewText } from "./noteBlocks";

const ADDED = 'My tv is 58"\n\n[addition 2026-09-15 01:24 UTC]\nActually it\'s 60"';

describe("reading a composed note back apart", () => {
  it("keeps the author's body byte-identical", () => {
    // `compose_body` appends and never moves a character of what was already there;
    // this is the reader's half of that guarantee.
    expect(parseNote(ADDED).body).toBe('My tv is 58"');
  });

  it("reads an addition as the owner's own words, with no label", () => {
    const { blocks } = parseNote(ADDED);
    expect(blocks).toEqual([
      { kind: "addition", at: "2026-09-15 01:24 UTC", answer: "Actually it's 60\"" },
    ]);
  });

  it("splits a clarification into the question and the answer", () => {
    const text =
      "I saw the cardiologist\n\n[clarification 2026-09-14 09:00 UTC]\nQ: Which one?\nA: Dr Ashcote";
    expect(parseNote(text).blocks).toEqual([
      {
        kind: "clarification",
        at: "2026-09-14 09:00 UTC",
        question: "Which one?",
        answer: "Dr Ashcote",
      },
    ]);
  });

  it("keeps a wrapped question whole", () => {
    // The question is whatever precedes the `A:` line, not line one — `ask_owner` text
    // wraps, and splitting on position would put half the question in the answer.
    const text =
      "note\n\n[clarification 2026-09-14 09:00 UTC]\nQ: Which one,\nthe Boulder one?\nA: That one";
    const [b] = parseNote(text).blocks;
    expect(b?.question).toBe("Which one,\nthe Boulder one?");
    expect(b?.answer).toBe("That one");
  });

  it("reads several blocks in order", () => {
    const text = `${ADDED}\n\n[addition 2026-09-15 12:09 UTC]\nAnd it's a Sony`;
    expect(parseNote(text).blocks.map((b) => b.answer)).toEqual([
      "Actually it's 60\"",
      "And it's a Sony",
    ]);
  });

  it("leaves a note that has no blocks entirely alone", () => {
    expect(parseNote("just a note")).toEqual({ body: "just a note", blocks: [] });
  });

  it("does not treat a marker the owner TYPED as structure", () => {
    // The literal is ordinary prose the moment it is not — he can paste a clarified
    // note into a new one. Pinning the stamp shape is what separates the two, and it is
    // the same reason `compose.strip_clarifications` refuses to cut on the literal.
    const typed = "I wrote [addition foo] in a note once";
    expect(parseNote(typed)).toEqual({ body: typed, blocks: [] });
    const nearly = "note\n\n[addition not-a-stamp]\nnope";
    expect(parseNote(nearly).blocks).toEqual([]);
  });
});

describe("what a clamped preview spends its lines on", () => {
  it("is the words, never the machine syntax", () => {
    // The reported defect: a two-line stream row spent both lines on
    // `[addition 2026-09-15 01:24 UTC]` and cut off before the sentence he typed.
    expect(previewText(ADDED)).toBe('My tv is 58"\nActually it\'s 60"');
    expect(previewText(ADDED)).not.toContain("[addition");
  });

  it("carries a clarification's ANSWER, which is the part he said", () => {
    const text = "note\n\n[clarification 2026-09-14 09:00 UTC]\nQ: Which one?\nA: Dr Ashcote";
    expect(previewText(text)).toBe("note\nDr Ashcote");
  });

  it("passes a plain note straight through", () => {
    expect(previewText("just a note")).toBe("just a note");
  });
});
