import { describe, expect, it } from "vitest";

import {
  DRAG_STEP_HZ,
  SSB_LOW_HZ,
  bandwidthAdjustable,
  bandwidthEdges,
  bandwidthLabel,
  bandwidthSpoken,
  pendingPassband,
  snapBandwidth,
  stepBandwidth,
  widthFromOffset,
} from "./sdrBandwidth";

const SSB = [3100, 2400, 1800];
// The ranges the box reports for these modes (deploy/sdr/demod.py BANDWIDTH_RANGE_HZ).
const AM_RANGE = [2000, 8000] as const;
const SSB_RANGE = [1000, 3100] as const;

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

  it("round-trips: an edge dragged to where a whole-kHz width sits asks for it", () => {
    for (const mode of ["am", "nfm", "usb", "lsb"]) {
      const [low, high] = mode === "usb" || mode === "lsb" ? SSB_RANGE : AM_RANGE;
      for (let width = low; width <= high; width += DRAG_STEP_HZ) {
        const { lowHz, highHz } = bandwidthEdges(mode, width);
        for (const edge of [lowHz, highHz]) {
          // The low edge of an SSB passband is the carrier edge, which is not draggable
          // and does not encode the width.
          if ((mode === "usb" && edge === lowHz) || (mode === "lsb" && edge === highHz)) continue;
          expect(snapBandwidth(widthFromOffset(mode, edge), low, high)).toBe(width);
        }
      }
    }
  });

  it("snaps a drag to whole kilohertz, which is what the owner asked for", () => {
    // 2 kHz apart was too coarse to place an edge beside a station; 100 Hz (the grid the
    // box accepts) is finer than a fingertip on a 32 kHz picture can mean.
    expect(snapBandwidth(6400, ...AM_RANGE)).toBe(6000);
    expect(snapBandwidth(6600, ...AM_RANGE)).toBe(7000);
    expect(snapBandwidth(5001, ...AM_RANGE)).toBe(5000);
  });

  it("clamps a drag past either end instead of refusing it", () => {
    // A finger past the edge of the picture is asking for the end of the range, not for
    // an error. Everywhere further in, an out-of-range width IS an error — there it
    // means a caller got it wrong rather than a thumb slipped.
    expect(snapBandwidth(99000, ...AM_RANGE)).toBe(8000);
    expect(snapBandwidth(10, ...AM_RANGE)).toBe(2000);
  });
});

describe("stepping with arrow keys", () => {
  it("moves one kilohertz, the same distance the drag snaps to", () => {
    // The arrows and the drag are one gesture at two resolutions. A key that jumped to
    // the next preset while the drag moved 1 kHz would make them disagree about what
    // "narrower" means.
    expect(stepBandwidth(8000, "narrower", ...AM_RANGE)).toBe(7000);
    expect(stepBandwidth(7000, "wider", ...AM_RANGE)).toBe(8000);
  });

  it("stops at the ends rather than wrapping", () => {
    // Wrapping would turn a held key into a jump from the filter that rejects
    // everything to the one that rejects nothing.
    expect(stepBandwidth(2000, "narrower", ...AM_RANGE)).toBeNull();
    expect(stepBandwidth(8000, "wider", ...AM_RANGE)).toBeNull();
  });

  it("lands ON the grid from an off-grid preset rather than carrying the remainder", () => {
    // NFM's 12.5k and SSB's 3.1k are real presets and neither is a whole kilohertz.
    expect(stepBandwidth(12500, "narrower", 5000, 16000)).toBe(12000);
    expect(stepBandwidth(12500, "wider", 5000, 16000)).toBe(13000);
    expect(stepBandwidth(3100, "narrower", ...SSB_RANGE)).toBe(3000);
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
  it("draws it for a mode with room in its range", () => {
    expect(
      bandwidthAdjustable({ bandwidth_hz: 8000, bandwidth_min_hz: 2000, bandwidth_max_hz: 8000 }),
    ).toBe(true);
  });

  it("hides it where there is nothing to choose", () => {
    // Wide FM: min === max, because narrowing clips the deviation.
    expect(
      bandwidthAdjustable({
        bandwidth_hz: 180000,
        bandwidth_min_hz: 180000,
        bandwidth_max_hz: 180000,
      }),
    ).toBe(false);
    // A spectrum stare has no channel for a filter to be.
    expect(bandwidthAdjustable({ bandwidth_hz: 0, bandwidth_min_hz: 0, bandwidth_max_hz: 0 })).toBe(
      false,
    );
    // ...and a sidecar older than the control sends none of the fields.
    expect(bandwidthAdjustable({})).toBe(false);
  });
});

describe("what the picture draws while a retune is in flight", () => {
  it("draws the chosen width, not the row's, until the row agrees", () => {
    // The bug the owner photographed: mode button reading 8k, shading still 16 kHz,
    // because every row arriving during the ~100 ms retune still carries the old
    // passband. Drawing from the row makes the picture contradict itself at exactly the
    // moment it is being watched to see whether the drag worked.
    expect(pendingPassband("am", 6000, 16000)).toEqual({ lowHz: -3000, highHz: 3000 });
  });

  it("stops overriding the moment the row catches up", () => {
    // Not "until a timer expires": the row itself is the signal, so the picture goes
    // back to describing itself as soon as it can.
    expect(pendingPassband("am", 6000, 6000)).toBeNull();
  });

  it("stops overriding when the box REFUSED the width", () => {
    // The session settles back on the old width, the drag state clears, `shownHz`
    // becomes that old width, and the row already agrees — so a refused width is never
    // left on screen as a lie about what the radio is doing.
    expect(pendingPassband("am", 8000, 8000)).toBeNull();
  });

  it("keeps SSB's override one-sided, like its passband", () => {
    expect(pendingPassband("lsb", 2400, 3100)).toEqual({ lowHz: -2700, highHz: -300 });
  });

  it("has nothing to say when there is no channel", () => {
    // A spectrum stare reports a zero width; there is no filter to draw.
    expect(pendingPassband("fm", 0, 0)).toBeNull();
  });
});
