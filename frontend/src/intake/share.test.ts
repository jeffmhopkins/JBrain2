import { describe, expect, it } from "vitest";
import { intakeShareUrl, parseIntakePath, parseIntakeSecret } from "./share";

describe("intake share helpers", () => {
  it("recognizes the /intake path (with or without a trailing slash)", () => {
    expect(parseIntakePath("/intake")).toBe(true);
    expect(parseIntakePath("/intake/")).toBe(true);
    expect(parseIntakePath("/intake/extra")).toBe(false);
    expect(parseIntakePath("/")).toBe(false);
    expect(parseIntakePath("/jcode/s/abc")).toBe(false);
  });

  it("reads the secret from the #t= fragment, else null", () => {
    expect(parseIntakeSecret("#t=abc123")).toBe("abc123");
    expect(parseIntakeSecret("#t=a%20b")).toBe("a b");
    expect(parseIntakeSecret("#")).toBeNull();
    expect(parseIntakeSecret("")).toBeNull();
    expect(parseIntakeSecret("#other=x")).toBeNull();
  });

  it("builds a copy-link with the secret in the fragment", () => {
    const url = intakeShareUrl("s3cr3t");
    expect(url).toContain("/intake#t=s3cr3t");
    expect(url.startsWith("http")).toBe(true);
  });
});
