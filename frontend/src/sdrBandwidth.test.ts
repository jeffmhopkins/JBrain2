import { describe, expect, it } from "vitest";

import {
  SSB_LOW_HZ,
  bandwidthAdjustable,
  bandwidthEdges,
  bandwidthLabel,
  bandwidthSpoken,
  nearestBandwidth,
  stepBandwidth,
  widthFromOffset,
} from "./sdrBandwidth";

const AM = [8000, 6000, 4000, 3000];
const SSB = [3100, 2400, 1800];

describe("where a passband sits", () => {
  it("centres a symmetric mode on the dial", () => {
    expect(bandwidthEdges("am", 6000)).toEqual({ lowHz: -3000, highHz: 3000 });
    expect(bandwidthEdges("nfm", 12500)).toEqual({ lowHz: -6250, highHz: 6250 });
  });

  it("keeps SSB one-sided, and narrows from the OUTER edge", () => {
    // The low edge is set by where the suppressed carrier sits, so it must not move as
    // the filter narrows — otherwise the passband walks onto the carrier.
    for (const width of SSB) {
      const usb = bandwidthEdges("usb", width);
      expect(usb.lowHz).toBe(SSB_LOW_HZ);
      expect(usb.highHz - usb.lowHz).toBe(width);
      // ...and lsb is the mirror, not a copy with a sign bolted on.
      expect(bandwidthEdges("lsb", width)).toEqual({ lowHz: -usb.highHz, highHz: -usb.lowHz });
    }
  });
});

describe("dragging an edge", () => {
  it("reads a symmetric drag as twice the offset", () => {
    expect(widthFromOffset("am", 3000)).toBe(6000);
    // Either handle asks the same question: dragging the lower edge of an AM filter and
    // dragging the upper one are one request.
    expect(widthFromOffset("am", -3000)).toBe(6000);
  });

  it("reads an SSB drag as the distance past the carrier edge", () => {
    expect(widthFromOffset("usb", SSB_LOW_HZ + 2400)).toBe(2400);
    expect(widthFromOffset("lsb", -(SSB_LOW_HZ + 2400))).toBe(2400);
  });

  it("round-trips: an edge dragged to where a rung sits asks for that rung", () => {
    for (const mode of ["am", "nfm", "usb", "lsb"]) {
      const ladder = mode === "usb" || mode === "lsb" ? SSB : AM;
      for (const width of ladder) {
        const { lowHz, highHz } = bandwidthEdges(mode, width);
        for (const edge of [lowHz, highHz]) {
          // The low edge of an SSB passband is the carrier edge, which is not draggable
          // and does not encode the width.
          if ((mode === "usb" && edge === lowHz) || (mode === "lsb" && edge === highHz)) continue;
          expect(nearestBandwidth(ladder, widthFromOffset(mode, edge))).toBe(width);
        }
      }
    }
  });

  it("snaps to the nearest rung rather than rounding to a step", () => {
    // The ladder is not evenly spaced, so a midpoint has to resolve by distance.
    expect(nearestBandwidth(AM, 6900)).toBe(6000);
    expect(nearestBandwidth(AM, 7100)).toBe(8000);
    // ...and a drag past either end lands on that end rather than off the ladder.
    expect(nearestBandwidth(AM, 99000)).toBe(8000);
    expect(nearestBandwidth(AM, 10)).toBe(3000);
  });

  it("has no rung to offer when the session sent no ladder", () => {
    expect(nearestBandwidth([], 4000)).toBe(0);
  });
});

describe("stepping with arrow keys", () => {
  it("walks the ladder in both directions", () => {
    expect(stepBandwidth(AM, 8000, "narrower")).toBe(6000);
    expect(stepBandwidth(AM, 6000, "wider")).toBe(8000);
  });

  it("stops at the ends rather than wrapping", () => {
    // Wrapping would turn a held key into a jump from the filter that rejects
    // everything to the one that rejects nothing.
    expect(stepBandwidth(AM, 3000, "narrower")).toBeNull();
    expect(stepBandwidth(AM, 8000, "wider")).toBeNull();
  });

  it("refuses to step from a width that is not on the ladder", () => {
    expect(stepBandwidth(AM, 5000, "narrower")).toBeNull();
  });
});

describe("labels", () => {
  it("keeps a whole number whole and a fractional one to one place", () => {
    expect(bandwidthLabel(8000)).toBe("8k");
    expect(bandwidthLabel(12500)).toBe("12.5k");
    expect(bandwidthLabel(180000)).toBe("180k");
    expect(bandwidthSpoken(2400)).toBe("2.4 kHz");
    expect(bandwidthSpoken(8000)).toBe("8 kHz");
  });
});

describe("whether to draw the control at all", () => {
  it("draws it for a mode with a real choice", () => {
    expect(bandwidthAdjustable({ bandwidth_hz: 8000, bandwidths_hz: AM })).toBe(true);
  });

  it("hides it where there is nothing to choose", () => {
    // Wide FM: one rung, because narrowing clips the deviation.
    expect(bandwidthAdjustable({ bandwidth_hz: 180000, bandwidths_hz: [180000] })).toBe(false);
    // A spectrum stare has no channel for a filter to be.
    expect(bandwidthAdjustable({ bandwidth_hz: 0, bandwidths_hz: [] })).toBe(false);
    // ...and a sidecar older than the control sends neither field.
    expect(bandwidthAdjustable({})).toBe(false);
  });
});
