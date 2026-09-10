// The recordings arithmetic against its binding spec (docs/mocks/recording/d-trim-sheet).
//
// What is pinned here is every number the owner is asked to ACT on. A trim discards the
// original and there is no undo, so "frees 509 kB" is not a decoration: it is the whole
// argument for pressing a button that destroys audio. And the cut lands on an MP3 frame
// boundary, so a control that implied finer precision than 72 ms would be promising
// something `-c copy` cannot deliver.

import { describe, expect, it } from "vitest";
import {
  FRAME_S,
  MIN_SELECTION_S,
  NOMINAL_BYTES_PER_S,
  bytesPerSecond,
  clampSelection,
  clockLabel,
  dayLabel,
  formatDuration,
  formatSize,
  formatStamp,
  groupByDay,
  isWholeClip,
  moveHandle,
  nudgeHandle,
  reclaimedLine,
  snapToFrame,
  trimGain,
  usageFraction,
  usageLine,
  waveformBars,
} from "./sdrTrim";

const KB = 1024;
const MB = 1024 * 1024;
const GB = 1024 * 1024 * 1024;

/** The mock's 3:06 weather clip, at the sidecar's real 8 kB/s. */
const CLIP_S = 186;
const CLIP_BYTES = CLIP_S * NOMINAL_BYTES_PER_S;

function onGrid(seconds: number): boolean {
  // Floating point: 0.072 does not divide evenly into anything, so "a multiple of a
  // frame" is a tolerance rather than a modulo.
  const frames = seconds / FRAME_S;
  return Math.abs(frames - Math.round(frames)) < 1e-6;
}

describe("frame boundaries", () => {
  it("rounds to the 72 ms grid a lossless cut can actually land on", () => {
    // 1.0 s is NOT a place ffmpeg can cut with -c copy; 14 frames is, and it is the
    // nearest one. Rounding rather than flooring, so a handle placed at 0.99 does not
    // silently move a whole frame away from the finger.
    expect(snapToFrame(1)).toBeCloseTo(14 * FRAME_S, 6);
    expect(snapToFrame(0.03)).toBe(0);
    expect(snapToFrame(0.05)).toBeCloseTo(FRAME_S, 6);
  });

  it("never publishes a position between two frames, however it was reached", () => {
    // Every route into a selection — a drag, an arrow key, a nudge — goes out through
    // the clamp, so this is the property that keeps the readout, the aria value and the
    // seconds sent to the server all naming the same cut.
    const dragged = moveHandle({ startS: 0, endS: CLIP_S }, "start", 12.3456, CLIP_S);
    const nudged = nudgeHandle(dragged, "end", -FRAME_S, CLIP_S);
    for (const at of [dragged.startS, nudged.startS, nudged.endS]) {
      expect(onGrid(at)).toBe(true);
    }
  });

  it("leaves the clip's own ends exactly where they are", () => {
    // 0 and the end of the file need no cut, so they are not rounded to the grid. Were
    // the end snapped DOWN, every sheet would open a frame short of the whole clip —
    // reading as a trim nobody asked for, and offering to destroy the original to free
    // a few hundred bytes. 186 s is deliberately NOT a whole number of frames.
    const opened = clampSelection({ startS: 0, endS: CLIP_S }, CLIP_S);
    expect(onGrid(CLIP_S)).toBe(false);
    expect(opened).toEqual({ startS: 0, endS: CLIP_S });
    expect(isWholeClip(opened, CLIP_S)).toBe(true);
  });
});

describe("clamping the handles", () => {
  it("reads a finger past the end of the picture as the end of the clip", () => {
    // A drag off the edge is asking for the edge, not for a refusal — the same reasoning
    // that clamps a bandwidth drag rather than erroring.
    expect(moveHandle({ startS: 0, endS: 10 }, "end", 9999, CLIP_S).endS).toBe(CLIP_S);
    expect(moveHandle({ startS: 5, endS: 10 }, "start", -9999, CLIP_S).startS).toBe(0);
  });

  it("will not let either handle cross the other", () => {
    const past = moveHandle({ startS: 0, endS: 10 }, "start", 40, CLIP_S);
    expect(past.startS).toBeLessThanOrEqual(past.endS - MIN_SELECTION_S);
    const before = moveHandle({ startS: 20, endS: 40 }, "end", 1, CLIP_S);
    expect(before.endS).toBeGreaterThanOrEqual(before.startS + MIN_SELECTION_S);
  });

  it("moves only the handle it was asked to move", () => {
    // Pushing the far edge along would let a drag past the other handle quietly shorten
    // the clip from an end the finger is nowhere near. 1389 frames is a real position,
    // so the far handle comes back byte-identical rather than merely close.
    const end = 1389 * FRAME_S;
    const moved = moveHandle({ startS: 10, endS: end }, "start", 20, CLIP_S);
    expect(moved.endS).toBe(end);
  });

  it("keeps a clip shorter than the minimum selection selectable", () => {
    // A two-second capture is a real thing to press stop on quickly, and a floor of half
    // a second must not produce a start past its own end on one.
    const tiny = clampSelection({ startS: 0, endS: 0.3 }, 0.3);
    expect(tiny.startS).toBe(0);
    expect(tiny.endS).toBeCloseTo(0.3, 6);
  });
});

describe("keyboard and nudge steps", () => {
  it("moves one frame at a time, which is the smallest honest step", () => {
    const from = { startS: 1.008, endS: 100 };
    expect(nudgeHandle(from, "start", FRAME_S, CLIP_S).startS).toBeCloseTo(1.08, 6);
    expect(nudgeHandle(from, "start", -FRAME_S, CLIP_S).startS).toBeCloseTo(0.936, 6);
  });

  it("stops at the ends rather than wrapping", () => {
    expect(nudgeHandle({ startS: 0, endS: 100 }, "start", -FRAME_S, CLIP_S).startS).toBe(0);
    expect(nudgeHandle({ startS: 0, endS: CLIP_S }, "end", FRAME_S, CLIP_S).endS).toBe(CLIP_S);
  });
});

describe("what a trim frees", () => {
  it("measures the clip's own rate instead of assuming the nominal bitrate", () => {
    // A stored file carries a header and whatever the last frame is, so a 42-second clip
    // is not exactly 336 kB. The sheet's figure has to survive comparison with the size
    // printed on the row it was opened from.
    expect(bytesPerSecond(400_000, 40)).toBe(10_000);
    // ...and falls back only where there is nothing to divide: a capture in flight.
    expect(bytesPerSecond(0, 0)).toBe(NOMINAL_BYTES_PER_S);
  });

  it("frees what the discarded seconds cost", () => {
    // The spec's own example: keep twenty seconds of a clip and the rest is the saving.
    const gain = trimGain({ startS: 20, endS: 40 }, CLIP_S, CLIP_BYTES);
    expect(gain.keptS).toBe(20);
    expect(gain.discardedS).toBe(CLIP_S - 20);
    expect(gain.keptBytes).toBe(20 * NOMINAL_BYTES_PER_S);
    expect(gain.freedBytes).toBe((CLIP_S - 20) * NOMINAL_BYTES_PER_S);
  });

  it("frees nothing when nothing is discarded", () => {
    // The one state where the sheet must NOT offer a saving: a trim that keeps the whole
    // clip deletes a blob and writes an identical one, which inverts the feature.
    const gain = trimGain({ startS: 0, endS: CLIP_S }, CLIP_S, CLIP_BYTES);
    expect(gain.freedBytes).toBe(0);
    expect(isWholeClip({ startS: 0, endS: CLIP_S }, CLIP_S)).toBe(true);
  });

  it("still counts the clip whole when a handle is one frame in", () => {
    // The handles move in frames, so an untouched selection can be a frame off the ends
    // after a round trip through the clamp; calling that a trim would offer to free
    // half a kilobyte and destroy the original for it.
    expect(isWholeClip({ startS: FRAME_S, endS: CLIP_S - FRAME_S }, CLIP_S)).toBe(true);
    expect(isWholeClip({ startS: 1, endS: CLIP_S }, CLIP_S)).toBe(false);
  });
});

describe("how the numbers are said", () => {
  it("prints a length the way the library and the sheet both print it", () => {
    expect(formatDuration(65)).toBe("1:05");
    expect(formatDuration(186)).toBe("3:06");
    expect(formatDuration(0)).toBe("0:00");
  });

  it("keeps a tenth on a handle, because a frame is smaller than a second", () => {
    // "0:12" and "0:12" for two positions 72 ms apart would make the nudge buttons look
    // broken.
    expect(formatStamp(65.28)).toBe("1:05.2");
    expect(formatStamp(0)).toBe("0:00.0");
  });

  it("scales a size so a row and the disk meter can be read against each other", () => {
    expect(formatSize(509 * KB)).toBe("509 kB");
    expect(formatSize(1.4 * MB)).toBe("1.4 MB");
    expect(formatSize(1.9 * GB)).toBe("1.9 GB");
  });

  it("says nothing expires until trimming has actually reclaimed something", () => {
    // There is no retention prune. The resting line must not imply one, and the traded
    // line must be earned: a box that has never trimmed has reclaimed nothing.
    expect(reclaimedLine(0)).toBe("kept until you delete them");
    expect(reclaimedLine(509 * KB)).toBe("509 kB reclaimed by trimming");
  });

  it("meters against a disk only when the box said how big it is", () => {
    expect(usageLine(1.9 * GB, 6, 8 * GB)).toBe("1.9 GB of 8.0 GB");
    expect(usageFraction(2 * GB, 8 * GB)).toBeCloseTo(0.25, 6);
    // Without a total there is no fraction to draw, and a made-up denominator would be
    // worse than a plain number — so the line degrades and the bar disappears.
    expect(usageLine(500 * KB, 1)).toBe("500 kB in 1 recording");
    expect(usageFraction(500 * KB, null)).toBeNull();
  });
});

describe("grouping the library by day", () => {
  const NOW = new Date("2026-09-10T18:00:00");
  const at = (iso: string, id: string) => ({ id, started_at: iso });

  it("names today and yesterday, and dates everything older", () => {
    expect(dayLabel("2026-09-10T23:45:00", NOW)).toBe("Today");
    expect(dayLabel("2026-09-09T02:11:00", NOW)).toBe("Yesterday");
    expect(dayLabel("2026-09-07T14:22:00", NOW)).toMatch(/7/);
  });

  it("orders newest first and heads each day exactly once", () => {
    // A clip unshifted locally after a capture arrives out of order relative to the
    // server's list, and one bad order puts a second "Today" halfway down the page.
    const groups = groupByDay(
      [
        at("2026-09-09T02:11:00", "old"),
        at("2026-09-10T09:00:00", "early"),
        at("2026-09-10T23:45:00", "late"),
      ],
      NOW,
    );
    expect(groups.map((g) => g.day)).toEqual(["Today", "Yesterday"]);
    expect(groups[0]?.rows.map((r) => r.id)).toEqual(["late", "early"]);
  });

  it("prints the row's clock in 24 hours, matching the frequency readout's register", () => {
    expect(clockLabel("2026-09-10T23:45:00")).toBe("23:45");
    expect(clockLabel("not a date")).toBe("");
  });
});

describe("the waveform", () => {
  it("keeps a short burst that averaging would erase", () => {
    // The picture's whole job is to show where the signal is against the dead air. A
    // four-second transmission inside three minutes of silence is exactly the thing the
    // owner is trimming TO, and a mean would flatten it into the noise around it.
    const peaks = new Array(200).fill(0.02);
    peaks[101] = 0.9;
    const bars = waveformBars(peaks, 20);
    expect(Math.max(...bars)).toBe(0.9);
  });

  it("draws the asked-for number of bars whatever the box stored", () => {
    expect(waveformBars([0.5, 0.5, 0.5], 116)).toHaveLength(116);
    expect(waveformBars(new Array(500).fill(0.5), 116)).toHaveLength(116);
    // A row with no envelope at all draws a flat line rather than nothing: an empty
    // picture reads as a failed sheet.
    expect(waveformBars([], 8)).toEqual(new Array(8).fill(0));
  });

  it("holds every bar inside the 0..1 the picture is drawn against", () => {
    expect(waveformBars([-3, 4], 2)).toEqual([0, 1]);
  });
});
