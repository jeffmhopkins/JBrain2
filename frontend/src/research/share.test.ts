import { describe, expect, it } from "vitest";
import { parseResearchSharePath, researchShareUrl } from "./share";

describe("researchShareUrl", () => {
  it("builds an origin-rooted /share/<token> link", () => {
    expect(researchShareUrl("9tK2pQ")).toBe(`${window.location.origin}/share/9tK2pQ`);
  });
  it("encodes the token", () => {
    expect(researchShareUrl("a/b c")).toContain("/share/a%2Fb%20c");
  });
});

describe("parseResearchSharePath", () => {
  it("reads the token from a /share/<token> path", () => {
    expect(parseResearchSharePath("/share/9tK2pQ")).toBe("9tK2pQ");
    expect(parseResearchSharePath("/share/9tK2pQ/")).toBe("9tK2pQ");
  });
  it("returns null on any other path (so the owner app still mounts)", () => {
    expect(parseResearchSharePath("/")).toBeNull();
    expect(parseResearchSharePath("/share/")).toBeNull();
    expect(parseResearchSharePath("/jcode/s/abc")).toBeNull();
    expect(parseResearchSharePath("/share/a/b")).toBeNull(); // token has no slash
  });
});
