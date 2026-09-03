// The roster's pure helpers.
//
// Small functions, but two of them decide whether the owner can find HIMSELF in a log
// that is three-quarters other people's traffic relayed by a machine — which is the
// problem this whole screen exists for.

import { describe, expect, it } from "vitest";
import { ago, arrival, baseCall, chipsFor, isMine, pinMine, shownLabel } from "./aprsStations";

function station(call: string, over: Record<string, unknown> = {}) {
  return {
    call,
    packets: 1,
    last_heard_at: new Date().toISOString(),
    kinds: ["Position"],
    gated: false,
    relay: null,
    ...over,
  } as Parameters<typeof pinMine>[0][number];
}

describe("whose station it is", () => {
  it("matches every SSID of the callsign the owner saved", () => {
    // The truck is -9 and the handheld is -7; they are one operator, and an owner who
    // typed the bare call in Settings meant both.
    expect(isMine("KE8XYZ-9", "KE8XYZ")).toBe(true);
    expect(isMine("ke8xyz-7", "KE8XYZ")).toBe(true);
    expect(isMine("KE8XYZ", "ke8xyz-9")).toBe(true);
    expect(isMine("KE8ABC-9", "KE8XYZ")).toBe(false);
  });

  it("matches NOTHING when no callsign is set", () => {
    // The dangerous direction: an empty setting that matched everything would tint and
    // pin the whole roster, which is worse than not knowing.
    expect(isMine("KE8XYZ-9", null)).toBe(false);
    expect(isMine("KE8XYZ-9", "   ")).toBe(false);
    expect(isMine("", "")).toBe(false);
  });

  it("pins the owner's stations without disturbing the recency order", () => {
    const rows = [station("N4TDX"), station("KE8XYZ-9"), station("W4ABC"), station("KE8XYZ-7")];

    expect(pinMine(rows, "KE8XYZ").map((s) => s.call)).toEqual([
      "KE8XYZ-9",
      "KE8XYZ-7",
      "N4TDX",
      "W4ABC",
    ]);
    // With no callsign set the server's order — most recently heard first — stands.
    expect(pinMine(rows, null).map((s) => s.call)).toEqual([
      "N4TDX",
      "KE8XYZ-9",
      "W4ABC",
      "KE8XYZ-7",
    ]);
  });
});

describe("how a station reached us", () => {
  it("separates heard-on-air from arrived-from-the-internet", () => {
    // Not cosmetic: one of these was a radio in range and the other never touched the
    // air near the box at all.
    expect(arrival({ gated: false, relay: null })).toBe("heard on RF");
    expect(arrival({ gated: true, relay: "N4TDX" })).toBe("gated via N4TDX");
  });

  it("does not name a relay it does not have", () => {
    // Reachable: a frame claiming an origin that is not callsign-shaped is filed under
    // the station that transmitted it, so there is no second station to name. Reading
    // "gated via " with nothing after it is the bug this rules out.
    expect(arrival({ gated: true, relay: null })).toBe("gated from the internet");
  });
});

describe("the chip row", () => {
  it("offers only the kinds that are actually there", () => {
    // Five chips where the range only holds objects is four dead controls.
    expect(chipsFor({ Object: 3, Position: 1 })).toEqual([
      { kind: "Position", count: 1 },
      { kind: "Object", count: 3 },
    ]);
  });

  it("collapses entirely when there is nothing to choose between", () => {
    // One chip is a filter with one option — it can only ever narrow to what is already
    // on screen.
    expect(chipsFor({ Object: 9 })).toEqual([]);
    expect(chipsFor({})).toEqual([]);
  });

  it("keeps a SELECTED kind on screen even when nothing here sends it", () => {
    // The dead end this closes: a Weather filter carried into a station that only sends
    // positions has a count of zero, so the chip disappeared — leaving an empty list, a
    // message saying "clear the type filter", and no filter on screen to clear.
    expect(chipsFor({ Position: 4 }, ["Weather"])).toEqual([
      { kind: "Position", count: 4 },
      { kind: "Weather", count: 0 },
    ]);
    // Even when it is the only chip: one control that is ON is not a dead control.
    expect(chipsFor({}, ["Object"])).toEqual([{ kind: "Object", count: 0 }]);
  });

  it("keeps a fixed order rather than following the counts", () => {
    // A control that reorders itself as traffic changes is a control you cannot aim.
    expect(chipsFor({ Other: 99, Position: 1, Weather: 40 }).map((c) => c.kind)).toEqual([
      "Position",
      "Weather",
      "Other",
    ]);
  });
});

describe("the list header", () => {
  it('reads "4 of 16" only while the chips are narrowing it', () => {
    expect(shownLabel(4, 16, true)).toBe("4 of 16");
    expect(shownLabel(16, 16, false)).toBe("16");
    // Filtering that happens to exclude nothing must not read as if it did.
    expect(shownLabel(16, 16, true)).toBe("16");
  });
});

describe("elapsed time", () => {
  it("rounds to one unit, because that is the question the roster answers", () => {
    const now = Date.parse("2026-09-03T12:00:00Z");
    const at = (ms: number) => new Date(now - ms).toISOString();

    expect(ago(at(22_000), now)).toBe("22s");
    expect(ago(at(4 * 60_000), now)).toBe("4m");
    expect(ago(at(3 * 3_600_000), now)).toBe("3h");
    expect(ago(at(2 * 86_400_000), now)).toBe("2d");
  });

  it("says nothing rather than NaN when a timestamp is unreadable", () => {
    // The timestamps come off the air. "NaNs ago" beside a callsign is the kind of
    // detail that makes an owner distrust the whole screen.
    expect(ago("not a date")).toBe("");
    expect(ago("")).toBe("");
  });

  it("never runs backwards on a clock skew", () => {
    const now = Date.parse("2026-09-03T12:00:00Z");
    // The box stamps `heard_at`; the browser reads it. A phone a few seconds fast
    // would otherwise show a negative age.
    expect(ago(new Date(now + 30_000).toISOString(), now)).toBe("0s");
  });
});

describe("base callsign", () => {
  it("survives the shapes a real callsign takes", () => {
    expect(baseCall("N4TDX-15")).toBe("N4TDX");
    expect(baseCall(" k4jtt-d ")).toBe("K4JTT");
    expect(baseCall("WINLINK")).toBe("WINLINK");
    expect(baseCall("")).toBe("");
  });
});
