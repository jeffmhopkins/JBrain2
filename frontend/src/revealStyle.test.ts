import { beforeEach, describe, expect, it } from "vitest";
import { REVEAL_STYLES, getRevealStyle, setRevealStyle } from "./revealStyle";

describe("revealStyle", () => {
  beforeEach(() => localStorage.clear());

  it("defaults to sweep when nothing is stored", () => {
    expect(getRevealStyle()).toBe("sweep");
  });

  it("round-trips a chosen style", () => {
    setRevealStyle("cascade");
    expect(getRevealStyle()).toBe("cascade");
  });

  it("falls back to the default for an unknown stored value", () => {
    localStorage.setItem("jbrain.revealStyle", "bogus");
    expect(getRevealStyle()).toBe("sweep");
  });

  it("offers exactly instant, cascade, and sweep", () => {
    expect(REVEAL_STYLES).toEqual(["instant", "cascade", "sweep"]);
  });
});
