import { describe, expect, it } from "vitest";
import { chatShareUrl, parseChatSharePath } from "./chatShare";

describe("chat share paths", () => {
  it("parses a token off /chat/s/{token}", () => {
    expect(parseChatSharePath("/chat/s/abc_DEF-123")).toBe("abc_DEF-123");
    expect(parseChatSharePath("/chat/s/abc/")).toBe("abc");
  });

  it("rejects every other path", () => {
    expect(parseChatSharePath("/share/abc")).toBeNull();
    expect(parseChatSharePath("/chat/s/")).toBeNull();
    expect(parseChatSharePath("/chat/s/a.b")).toBeNull();
    expect(parseChatSharePath("/chat/s/abc/extra")).toBeNull();
  });

  it("builds the copy-link on this origin", () => {
    expect(chatShareUrl("tok")).toBe(`${window.location.origin}/chat/s/tok`);
  });
});
