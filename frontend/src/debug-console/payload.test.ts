import { describe, expect, it } from "vitest";

import { decodeToken } from "./payload";

// Mirror the server's build_debug_payload: base64url(JSON), no padding.
function encode(obj: unknown): string {
  return btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

describe("decodeToken", () => {
  it("decodes a minted payload into base + key, trimming a trailing slash", () => {
    const payload = encode({ v: 1, u: "https://brain.example.com/", k: "SECRET-KEY" });
    expect(decodeToken(payload)).toEqual({ base: "https://brain.example.com", key: "SECRET-KEY" });
  });

  it("tolerates a leading '#' from a URL fragment and surrounding whitespace", () => {
    const payload = encode({ v: 1, u: "https://x.test", k: "abc" });
    expect(decodeToken(`#${payload}  `)).toEqual({ base: "https://x.test", key: "abc" });
  });

  it("returns null for empty, malformed, or incomplete payloads", () => {
    expect(decodeToken("")).toBeNull();
    expect(decodeToken("not-base64-$$$")).toBeNull();
    expect(decodeToken(encode({ v: 1, u: "https://x.test" }))).toBeNull(); // no key
    expect(decodeToken(encode({ k: "abc" }))).toBeNull(); // no url
  });
});
