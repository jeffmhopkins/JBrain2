import { beforeEach, describe, expect, it } from "vitest";
import { TOKEN_RATES, getTokenRate, setTokenRate } from "./tokenRate";

describe("tokenRate", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("defaults to 30 tokens/sec when unset", () => {
    expect(getTokenRate()).toBe(30);
  });

  it("persists and reads back a chosen rate", () => {
    setTokenRate(45);
    expect(localStorage.getItem("jbrain.tokenRate")).toBe("45");
    expect(getTokenRate()).toBe(45);
  });

  it("treats 0 (instant) as a valid stored value, not a missing one", () => {
    setTokenRate(0);
    expect(getTokenRate()).toBe(0);
  });

  it("falls back to the default on garbage storage", () => {
    localStorage.setItem("jbrain.tokenRate", "999");
    expect(getTokenRate()).toBe(30);
    localStorage.setItem("jbrain.tokenRate", "fast");
    expect(getTokenRate()).toBe(30);
  });

  it("offers instant plus four speeds", () => {
    expect(TOKEN_RATES).toEqual([0, 20, 30, 45, 60]);
  });
});
