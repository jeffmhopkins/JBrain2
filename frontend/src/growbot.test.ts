import { describe, expect, it } from "vitest";
import { byDay, dayHeading, framesHere } from "./growbot";

// Local-time instants, so the day boundaries hold in any time zone the tests run in.
const NOW = new Date(2026, 10, 12, 9, 30);
const at = (d: number, h: number) => new Date(2026, 10, d, h, 0).toISOString();

describe("dayHeading", () => {
  it("names today and yesterday by local day, and older days by date", () => {
    expect(dayHeading(at(12, 0), NOW)).toBe("Today");
    expect(dayHeading(at(11, 23), NOW)).toBe("Yesterday");
    expect(dayHeading(at(10, 12), NOW)).toBe("10 Nov");
  });

  it("names the year of an older year's day, so the same date a year apart never merges", () => {
    const lastYear = new Date(2025, 10, 12, 9, 0).toISOString();
    expect(dayHeading(lastYear, NOW)).toBe("12 Nov 2025");
    const rows = [at(12, 8), { created_at: lastYear }].map((r) =>
      typeof r === "string" ? { created_at: r } : r,
    );
    expect(byDay(rows, NOW).map((g) => g.heading)).toEqual(["Today", "12 Nov 2025"]);
  });
});

describe("byDay", () => {
  it("keeps the order given and starts a group at each new day", () => {
    const rows = [at(12, 8), at(12, 7), at(11, 20), at(10, 14)].map((created_at) => ({
      created_at,
    }));
    expect(byDay(rows, NOW).map((g) => [g.heading, g.rows.length])).toEqual([
      ["Today", 2],
      ["Yesterday", 1],
      ["10 Nov", 1],
    ]);
    expect(byDay([], NOW)).toEqual([]);
  });
});

describe("framesHere", () => {
  const at = (hostname: string, port = "") => ({ hostname, port });
  const phone = "Mozilla/5.0 (Linux; Android 14) Chrome/130";
  it("frames the creature only where the creature lets the app frame it", () => {
    expect(framesHere("https://jbrain.local:8444/open?t=1", at("jbrain.local"), phone)).toBe(true);
    expect(framesHere("https://growbot.example.com/open", at("example.com"), phone)).toBe(true);
    // The owner site's private name, and a subdomain PWA under the preview apex: elsewhere.
    expect(
      framesHere("https://growbot.example.com/open", at("owner.jbrain.home.arpa"), phone),
    ).toBe(false);
    expect(framesHere("https://growbot.example.com/open", at("jbrain.example.com"), phone)).toBe(
      false,
    );
    expect(framesHere("https://jbrain.local/open", at("jbrain.local"), phone)).toBe(false);
    expect(framesHere("https://jbrain.local:8444/open", at("jbrain.local", "8443"), phone)).toBe(
      false,
    );
    expect(framesHere("https://notexample.com/open", at("example.com"), phone)).toBe(false);
    expect(framesHere("http://jbrain.local:8444/open", at("jbrain.local"), phone)).toBe(false);
    expect(framesHere("not a url", at("jbrain.local"), phone)).toBe(false);
    expect(framesHere("https://jbrain.local:8444/open", at(""), phone)).toBe(false);
  });

  it("never frames in the owner app, whose WebView grants a frame no camera", () => {
    const owner = `${phone} JBrainOwner/1.4`;
    expect(framesHere("https://jbrain.local:8444/open", at("jbrain.local"), owner)).toBe(false);
  });
});
