import { describe, expect, it } from "vitest";
import { jlaunchTerminalWsUrl } from "./terminal";

describe("jlaunchTerminalWsUrl", () => {
  it("builds a same-origin ws URL for a job's PTY", () => {
    expect(jlaunchTerminalWsUrl("abc123")).toBe(
      `ws://${window.location.host}/api/jlaunch/jobs/abc123/terminal`,
    );
  });
  it("encodes the job id", () => {
    expect(jlaunchTerminalWsUrl("a/b")).toContain("/api/jlaunch/jobs/a%2Fb/terminal");
  });
});
