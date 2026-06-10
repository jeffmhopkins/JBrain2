import { beforeEach, describe, expect, it } from "vitest";
import { FONT_SCALES, getFontScale, initFontScale, setFontScale } from "./fontScale";

function appliedScale(): string {
  return document.documentElement.style.getPropertyValue("--font-scale");
}

describe("fontScale", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.style.removeProperty("--font-scale");
  });

  it("defaults to 75%", () => {
    expect(getFontScale()).toBe(75);
    initFontScale();
    expect(appliedScale()).toBe("0.75");
  });

  it("persists and applies a chosen scale", () => {
    setFontScale(100);
    expect(appliedScale()).toBe("1");
    expect(getFontScale()).toBe(100);
    initFontScale();
    expect(appliedScale()).toBe("1");
  });

  it("falls back to default on garbage storage", () => {
    localStorage.setItem("jbrain.fontScale", "999");
    expect(getFontScale()).toBe(75);
  });

  it("offers the four supported steps", () => {
    expect(FONT_SCALES).toEqual([65, 75, 90, 100]);
  });
});
