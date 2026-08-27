import { describe, expect, it } from "vitest";

import { inertText, isBodylessPost, outboxBody, outboxPreview } from "./moltbookSafe";

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

  it("defangs script/data schemes to match the server", () => {
    expect(inertText("javascript:alert(1)")).toBe("x-javascript:alert(1)");
    expect(inertText("data:text/html,x")).toBe("x-data:text/html,x");
    expect(inertText("mailto:a@b.co")).toBe("x-mailto:a@b.co");
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

describe("outboxBody", () => {
  // The review queue is the release gate, and it used to render a bodyless post and a
  // six-hundred-word one identically — `outboxPreview` returns the first non-empty field,
  // which for a post is always the title. One bodyless post was released to the live site
  // that way, with nothing on screen to distinguish it.
  it("returns the body, not the title", () => {
    expect(outboxBody({ title: "A headline", content: "The actual thinking." })).toBe(
      "The actual thinking.",
    );
  });

  it("is empty for a bare-title post", () => {
    expect(outboxBody({ title: "A headline", content: "" })).toBe("");
    expect(outboxBody({ title: "A headline" })).toBe("");
  });

  it("does not repeat the title when the body duplicates it", () => {
    expect(outboxBody({ title: "same", content: "same" })).toBe("");
  });

  it("defangs links in the body like everything else the owner is shown", () => {
    expect(outboxBody({ title: "t", content: "see https://scam.example" })).toBe(
      "see hxxps://scam.example",
    );
  });
});

describe("isBodylessPost", () => {
  it("flags a post with no body", () => {
    expect(isBodylessPost("post", { title: "A headline", content: "" })).toBe(true);
    expect(isBodylessPost("post", { title: "A headline" })).toBe(true);
    expect(isBodylessPost("post", { title: "A headline", content: "   \n " })).toBe(true);
  });

  it("does not flag a post with a real body", () => {
    expect(isBodylessPost("post", { title: "t", content: "a body" })).toBe(false);
  });

  it("flags a body that just repeats the title", () => {
    // outboxBody suppresses it as a duplicate, so the row renders identically to a bodyless
    // one. Flagging only the empty case would leave this releasable.
    expect(isBodylessPost("post", { title: "same", content: "same" })).toBe(true);
    expect(isBodylessPost("post", { title: " same ", content: "same" })).toBe(true);
  });

  it("only applies to posts", () => {
    // A vote or a follow legitimately carries no body; flagging those would train the
    // owner to ignore the warning.
    expect(isBodylessPost("vote", { target_id: "t1" })).toBe(false);
    expect(isBodylessPost("follow", { name: "Luna24" })).toBe(false);
    expect(isBodylessPost("comment", { post_id: "p1", content: "hi" })).toBe(false);
  });
});
