import { describe, expect, it } from "vitest";
import { fmtEta, jlaunchLiveUrl, jlaunchResultUrl } from "./live";

describe("jlaunchLiveUrl", () => {
  it("points into the job's live folder, escaping both parts", () => {
    expect(jlaunchLiveUrl("j1", "replay.html")).toBe("/api/jlaunch/jobs/j1/live/replay.html");
    expect(jlaunchLiveUrl("a/b", "x y.json")).toBe("/api/jlaunch/jobs/a%2Fb/live/x%20y.json");
  });
  it("asks for an attachment when downloading", () => {
    expect(jlaunchLiveUrl("j1", "policy_latest.json", true)).toBe(
      "/api/jlaunch/jobs/j1/live/policy_latest.json?download=1",
    );
  });
});

describe("jlaunchResultUrl", () => {
  it("is the owner download route", () => {
    expect(jlaunchResultUrl("j1")).toBe("/api/jlaunch/jobs/j1/artifact");
  });
});

describe("fmtEta", () => {
  it("reads naturally at each scale", () => {
    expect(fmtEta(30)).toBe("under 2 min left");
    expect(fmtEta(600)).toBe("about 10 min left");
    expect(fmtEta(3600)).toBe("about 1 h left");
    expect(fmtEta(4800)).toBe("about 1 h 20 min left");
  });
});
