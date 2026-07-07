import { describe, expect, it } from "vitest";
import { readingProfile, speakable, toProse, toUtterance } from "./speakable.js";

// Golden read-aloud corpus — the audiobook-plan regression net (docs/archive/READ_ALOUD_AUDIOBOOK_PLAN.md).
// These lock the CURRENT normalized output for a short-story excerpt and a Markdown answer, so any
// later prosody/pronunciation change (misaki, pacing, lexicon) shows up as an INTENTIONAL diff here
// rather than silently. Known warts are captured as-is on purpose — e.g. "1.5s" -> "one.5s" (a
// number with a stuck-on unit) is today's behavior and a later wave's fix; when it changes, this
// baseline changes with it.

const STORY = `Mr. Alder paused at the door. "You're late," she said—again.

He counted: 3 excuses, none good... He said nothing.`;

const ANSWER = `## Summary

The API returned **200 OK** in \`1.5s\`. Key points:

- Latency dropped 20%.
- See [the docs](https://example.com/guide) for details.

Steps:

1. Restart the service.
2. Re-run the check.`;

describe("speakable golden corpus", () => {
  it("normalizes a short-story excerpt (dialogue, em-dash, ellipsis, title, paragraph break)", () => {
    expect(speakable(STORY)).toBe(
      'Mister Alder paused at the door. "You\'re late," she said, again. He counted: three excuses, none good… He said nothing.',
    );
  });

  it("normalizes a Markdown LLM answer (heading, lists, bold, inline code, link, table-free)", () => {
    expect(speakable(ANSWER)).toBe(
      "Summary. The API returned two hundred OK in one.5s. Key points: Latency dropped twenty percent. See the docs for details. Steps: one. Restart the service. two. Re run the check.",
    );
  });

  it("toProse keeps structure (newlines) and defers pronunciation to the utterance pass", () => {
    // Structural pass: markdown/markers gone, numbers/percent still raw, newlines preserved.
    expect(toProse(ANSWER)).toBe(
      [
        "Summary",
        "",
        "The API returned 200 OK in 1.5s. Key points:",
        "",
        "Latency dropped 20%.",
        "See the docs for details.",
        "",
        "Steps:",
        "",
        "one. Restart the service.",
        "two. Re-run the check.",
      ].join("\n"),
    );
    // Composing the two passes equals the one-shot entry point.
    expect(toUtterance(toProse(ANSWER))).toBe(speakable(ANSWER));
  });

  it("kokoro and piper share one ruleset today (the seam is wired but not yet diverged)", () => {
    // W1 gives kokoro its own misaki-aware profile; until then output must match byte-for-byte.
    expect(speakable(STORY, "kokoro")).toBe(speakable(STORY, "piper"));
    expect(speakable(ANSWER, "kokoro")).toBe(speakable(ANSWER, "piper"));
  });
});

describe("readingProfile (markup vs prose)", () => {
  it("reads the story excerpt as prose and the Markdown answer as markup", () => {
    expect(readingProfile(STORY)).toBe("prose");
    expect(readingProfile(ANSWER)).toBe("markup");
  });

  it("classifies structural / code / dense-inline Markdown as markup", () => {
    expect(readingProfile("## Heading\n\nsome text")).toBe("markup"); // heading
    expect(readingProfile("- one\n- two\n- three")).toBe("markup"); // list
    expect(readingProfile("intro\n\n```\ncode()\n```")).toBe("markup"); // fenced code
    expect(readingProfile("| a | b |\n|---|---|\n| 1 | 2 |")).toBe("markup"); // table
    expect(readingProfile("**a** **b** `c` [d](x)")).toBe("markup"); // dense inline
  });

  it("classifies plain paragraphs and light dialogue as prose", () => {
    expect(readingProfile("The wind rose. She waited by the door, and said nothing.")).toBe(
      "prose",
    );
    expect(readingProfile('He turned. "You came," she said. A single *beat* hung there.')).toBe(
      "prose",
    );
    expect(readingProfile("")).toBe("prose"); // blank → the gentle default
  });
});
