import { describe, expect, it } from "vitest";

import { inertText, outboxPreview } from "./moltbookSafe";

describe("inertText", () => {
  it("strips invisible and bidi characters", () => {
    // Built from codepoints (pure-ASCII source): ZWSP, RLO, a Unicode Tag char, and BOM.
    const zwsp = String.fromCodePoint(0x200b);
    const rlo = String.fromCodePoint(0x202e);
    const tag = String.fromCodePoint(0xe0041);
    const bom = String.fromCodePoint(0xfeff);
    expect(inertText(`hel${zwsp}lo${rlo}world${tag}`)).toBe("helloworld");
    expect(inertText(`a${bom}b`)).toBe("ab");
  });

  it("defangs URL schemes without touching the host", () => {
    expect(inertText("see https://evil.example/x")).toBe("see hxxps://evil.example/x");
    expect(inertText("HTTP://Bad.example")).toBe("hxxp://Bad.example");
  });

  it("leaves ordinary text alone", () => {
    expect(inertText("the general submolt is mostly noise")).toBe(
      "the general submolt is mostly noise",
    );
  });

  it("handles nullish input", () => {
    expect(inertText(null)).toBe("");
    expect(inertText(undefined)).toBe("");
  });
});

describe("outboxPreview", () => {
  it("prefers the title, made inert", () => {
    expect(outboxPreview({ title: "buy https://scam.example", content: "x" })).toBe(
      "buy hxxps://scam.example",
    );
  });

  it("falls back to content, then to a post id", () => {
    expect(outboxPreview({ content: "hi there" })).toBe("hi there");
    expect(outboxPreview({ post_id: "p1" })).toBe("p1");
  });

  it("is empty when nothing renderable is present", () => {
    expect(outboxPreview({ up: true })).toBe("");
  });
});
