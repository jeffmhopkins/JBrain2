// Range and bearing from the phone's own position.
//
// Two things are pinned here, and neither is the arithmetic. The first is that the
// coordinate NEVER LEAVES THE DEVICE — every function is pure and local, so there is no
// request to make. The second is that a refusal is a normal outcome: the geolocation
// prompt is declined more often than granted, and the row must fall through to the grid
// square rather than wait or throw.

import { describe, expect, it, vi } from "vitest";
import { type Fix, askWhereYouAre, rangeAndBearing, rangeLine } from "./whereYouAre";

const HOME: Fix = { lat: 28.61, lon: -80.81, accuracy: 20 };

describe("range and bearing", () => {
  it("measures a station the box actually heard", () => {
    // KC3EFJ, from the owner's channel.
    const { miles, point } = rangeAndBearing(HOME, 28.6212, -80.8237);

    expect(miles).toBeCloseTo(1.1, 1);
    expect(point).toBe("NW");
  });

  it("gets the compass right in each quadrant", () => {
    expect(rangeAndBearing(HOME, 29.61, -80.81).point).toBe("N");
    expect(rangeAndBearing(HOME, 27.61, -80.81).point).toBe("S");
    expect(rangeAndBearing(HOME, 28.61, -79.81).point).toBe("E");
    expect(rangeAndBearing(HOME, 28.61, -81.81).point).toBe("W");
  });

  it("is zero at the reader's own position rather than NaN", () => {
    // `asin` of a hair over 1.0 is NaN, and a floating-point round trip can produce
    // exactly that for a point compared with itself.
    const { miles } = rangeAndBearing(HOME, HOME.lat, HOME.lon);

    expect(Number.isNaN(miles)).toBe(false);
    expect(miles).toBe(0);
  });
});

describe("the line as it reads on a row", () => {
  it("keeps a tenth of a mile close in and drops it further out", () => {
    // "12.3 mi" from a consumer GPS claims a metre of certainty nobody has.
    expect(rangeLine(HOME, 28.6212, -80.8237)).toBe("1.1 mi NW");
    expect(rangeLine(HOME, 29.61, -80.81)).toBe("69 mi N");
  });

  it("says 'here' rather than inventing a precision the fix cannot support", () => {
    // A station 40 m away, measured with a 3 km fix, is not 0.02 miles away — it is
    // somewhere in this town, and the row must not pretend otherwise.
    const vague: Fix = { lat: 28.61, lon: -80.81, accuracy: 3000 };

    expect(rangeLine(vague, 28.6105, -80.8105)).toBe("here");
  });
});

describe("asking the browser", () => {
  it("returns a fix when the reader allows it", async () => {
    vi.stubGlobal("navigator", {
      geolocation: {
        getCurrentPosition: (ok: PositionCallback) =>
          ok({
            coords: { latitude: 28.61, longitude: -80.81, accuracy: 12 },
          } as GeolocationPosition),
      },
    });

    await expect(askWhereYouAre()).resolves.toEqual({ lat: 28.61, lon: -80.81, accuracy: 12 });
    vi.unstubAllGlobals();
  });

  it("resolves to null when the reader refuses, rather than rejecting", async () => {
    // Refusal is the COMMON case, not an error. A rejected promise here would surface
    // as an unhandled rejection on a screen where nothing has gone wrong.
    vi.stubGlobal("navigator", {
      geolocation: {
        getCurrentPosition: (_ok: PositionCallback, fail: PositionErrorCallback) =>
          fail({ code: 1, message: "denied" } as GeolocationPositionError),
      },
    });

    await expect(askWhereYouAre()).resolves.toBeNull();
    vi.unstubAllGlobals();
  });

  it("gives up rather than hanging when the prompt is dismissed", async () => {
    // Some browsers call NEITHER callback when a permission prompt is dismissed instead
    // of answered. Without the timer the row waits for ever on a promise that cannot
    // reject, and the fallback never runs.
    vi.useFakeTimers();
    vi.stubGlobal("navigator", { geolocation: { getCurrentPosition: () => {} } });

    const pending = askWhereYouAre(500);
    await vi.advanceTimersByTimeAsync(600);

    await expect(pending).resolves.toBeNull();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("returns null where the browser has no geolocation at all", async () => {
    vi.stubGlobal("navigator", {});

    await expect(askWhereYouAre()).resolves.toBeNull();
    vi.unstubAllGlobals();
  });
});
