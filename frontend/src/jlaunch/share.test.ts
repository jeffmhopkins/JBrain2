import { describe, expect, it } from "vitest";
import { jlaunchArtifactUrl, jlaunchShareUrl, parseJlaunchSharePath } from "./share";

describe("jlaunchShareUrl", () => {
  it("builds an origin-rooted /results link", () => {
    expect(jlaunchShareUrl("9f2a1c")).toBe(`${window.location.origin}/results/9f2a1c`);
  });
  it("encodes the token", () => {
    expect(jlaunchShareUrl("a/b")).toContain("/results/a%2Fb");
  });
});

describe("jlaunchArtifactUrl", () => {
  it("points at the public download endpoint", () => {
    expect(jlaunchArtifactUrl("tok")).toBe("/api/jlaunch-share/tok/artifact");
  });
});

describe("parseJlaunchSharePath", () => {
  it("reads the token from a /results path", () => {
    expect(parseJlaunchSharePath("/results/9f2a1c7b")).toBe("9f2a1c7b");
    expect(parseJlaunchSharePath("/results/9f2a1c7b/")).toBe("9f2a1c7b");
  });
  it("returns null on any other path", () => {
    expect(parseJlaunchSharePath("/home")).toBeNull();
    expect(parseJlaunchSharePath("/results/")).toBeNull();
    expect(parseJlaunchSharePath("/share/abc")).toBeNull();
  });
});
