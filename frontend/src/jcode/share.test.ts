import { describe, expect, it } from "vitest";
import { parseShareLink, shareUrl } from "./share";

describe("shareUrl", () => {
  it("builds an origin-rooted link with the secret in the fragment", () => {
    const url = shareUrl("abc123", "s3cret");
    expect(url).toBe(`${window.location.origin}/jcode/s/abc123#t=s3cret`);
  });
  it("encodes the id and token", () => {
    expect(shareUrl("a/b", "x y")).toContain("/jcode/s/a%2Fb#t=x%20y");
  });
});

describe("parseShareLink", () => {
  it("reads {sid, token} from a share path + fragment", () => {
    expect(parseShareLink("/jcode/s/abc123", "#t=s3cret")).toEqual({
      sid: "abc123",
      token: "s3cret",
    });
  });
  it("returns null without a token fragment", () => {
    expect(parseShareLink("/jcode/s/abc123", "")).toBeNull();
  });
  it("returns null on any other path", () => {
    expect(parseShareLink("/home", "#t=s3cret")).toBeNull();
    expect(parseShareLink("/jcode/s/", "#t=s3cret")).toBeNull();
  });
});
