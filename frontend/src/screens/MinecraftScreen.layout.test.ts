// jsdom computes no layout, so this guards the rule itself. Without it the screen's flex
// column shrank every overflow:hidden card to fit a phone's height instead of scrolling,
// and Update Minecraft, the changelog and the players were cut off (review B1).

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const CSS = readFileSync("src/styles.css", "utf8");

function rule(selector: string): string {
  const at = CSS.indexOf(`${selector} {`);
  expect(at, `${selector} is missing`).toBeGreaterThan(-1);
  return CSS.slice(at, CSS.indexOf("}", at));
}

describe("the Minecraft screen's layout", () => {
  it("scrolls inside the screen body", () => {
    expect(rule(".screen-body")).toMatch(/overflow-y:\s*auto/);
  });

  it("never lets a section shrink to fit", () => {
    expect(rule(".mc-screen > *")).toMatch(/flex-shrink:\s*0/);
  });
});
