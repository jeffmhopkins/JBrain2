import { describe, expect, it } from "vitest";
import { safeHref } from "./safeHref";

describe("safeHref", () => {
  it.each([
    "javascript:alert(1)",
    "JAVASCRIPT:alert(1)",
    "JaVaScRiPt:alert(1)",
    "  javascript:alert(1)",
    "\tjava\nscript:alert(1)",
    "\u0000javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "blob:https://example.com/abc",
  ])("refuses %j", (url) => {
    expect(safeHref(url)).toBeUndefined();
  });

  it.each([
    "https://meet.example.com/abc?x=1",
    "http://example.com",
    "HTTPS://Example.com/a",
    "  https://example.com/a  ",
    "mailto:someone@example.com",
  ])("allows %j unchanged", (url) => {
    expect(safeHref(url)).toBe(url);
  });

  it.each(["/notes/1", "notes/1", "//evil.example.com/x", "", null, undefined])(
    "refuses relative or empty %j (data URLs must be absolute)",
    (url) => {
      expect(safeHref(url)).toBeUndefined();
    },
  );
});
