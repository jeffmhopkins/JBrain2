import { describe, expect, it } from "vitest";
import { reportSections } from "./reportSections";

describe("reportSections", () => {
  it("splits a briefing into one story per ## heading, lead first", () => {
    const md = [
      "# Daily briefing",
      "Good morning — here is today.",
      "## Top **national** headlines",
      "Congress passed a bill.[^1]",
      "### Detail",
      "More on the bill.",
      "## Space Coast",
      "A launch is set for Tuesday.",
    ].join("\n");
    const out = reportSections(md);
    expect(out.map((s) => s.title)).toEqual([
      "Daily briefing",
      "Top national headlines",
      "Space Coast",
    ]);
    // Deeper headings stay inside their story and are dropped from what gets spoken.
    expect(out[1]?.md).toContain("More on the bill.");
    expect(out[1]?.md).not.toContain("### Detail");
    expect(out.every((s) => s.seconds > 0)).toBe(true);
  });

  it("drops the Sources section and headings with nothing under them", () => {
    const md =
      "## Empty\n\n## Story\nIt happened.\n## Sources\n1. https://example.com\n## After\nNo.";
    const out = reportSections(md);
    expect(out.map((s) => s.title)).toEqual(["Story"]);
  });

  it("keeps a heading-less report as a single untitled story", () => {
    const out = reportSections("Just one paragraph of findings.");
    expect(out).toHaveLength(1);
    expect(out[0]?.title).toBe("");
  });

  it("weights longer stories longer", () => {
    const [a, b] = reportSections(`## A\n${"word ".repeat(20)}\n## B\n${"word ".repeat(200)}`);
    expect((b?.seconds ?? 0) > (a?.seconds ?? 0)).toBe(true);
  });
});
