// The band-pick history: device-local, best-effort, and never allowed to break the
// picker it feeds. Every case here is a way the store can be WRONG rather than absent —
// JSON somebody else wrote, a value that is not a date, a section that no longer
// exists — because the failure mode is a sheet that cannot be opened at all.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BAND_PICKS_KEY, MAX_PICKS, loadPicks, notePick, recentlyPicked } from "./sdrBandPicks";

const T = (iso: string) => new Date(iso);

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("remembering a pick", () => {
  it("writes it and hands back the map the sheet redraws from", () => {
    const picks = notePick("40m", T("2026-09-07T12:00:00Z"));

    expect(picks["40m"]).toBe("2026-09-07T12:00:00.000Z");
    expect(loadPicks()).toEqual(picks);
  });

  it("keeps the newest pick of a band, not the first", () => {
    notePick("40m", T("2026-09-01T12:00:00Z"));

    const picks = notePick("40m", T("2026-09-07T12:00:00Z"));

    expect(picks["40m"]).toBe("2026-09-07T12:00:00.000Z");
  });

  it("caps the history so a key that only grows cannot", () => {
    for (let i = 0; i < MAX_PICKS + 6; i++) {
      notePick(`band-${i}`, T(`2026-09-07T12:${String(i).padStart(2, "0")}:00Z`));
    }

    const picks = loadPicks();
    expect(Object.keys(picks)).toHaveLength(MAX_PICKS);
    // The OLDEST are the ones dropped: a band tuned this minute must survive a cap.
    expect(picks["band-0"]).toBeUndefined();
    expect(picks[`band-${MAX_PICKS + 5}`]).toBeDefined();
  });

  it("survives storage that refuses to be written", () => {
    // Private mode, or a browser with site data blocked. The reorder the owner just
    // watched happen is still returned; it simply does not outlive the sheet.
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });

    expect(() => notePick("40m")).not.toThrow();
    expect(notePick("40m")["40m"]).toBeDefined();
  });
});

describe("reading the history back", () => {
  it("is empty on a device that has never picked one", () => {
    expect(loadPicks()).toEqual({});
  });

  it("treats unreadable JSON as no history rather than throwing", () => {
    localStorage.setItem(BAND_PICKS_KEY, "{not json");

    expect(loadPicks()).toEqual({});
  });

  it("drops values that are not dates instead of sorting on them", () => {
    // One `null` reaching `Date.parse` sorts the whole list into nonsense, and the
    // owner would see a Recent group in an order nothing explains.
    localStorage.setItem(
      BAND_PICKS_KEY,
      JSON.stringify({ "40m": "2026-09-07T12:00:00Z", "80m": null, "20m": "whenever" }),
    );

    expect(loadPicks()).toEqual({ "40m": "2026-09-07T12:00:00Z" });
  });

  it("refuses a payload that is not a map at all", () => {
    localStorage.setItem(BAND_PICKS_KEY, JSON.stringify(["40m", "80m"]));

    expect(loadPicks()).toEqual({});
  });
});

describe("which bands lead the list", () => {
  const sections = [{ id: "40m" }, { id: "80m" }, { id: "20m" }, { id: "cb" }];

  it("orders them by when they were picked, newest first", () => {
    const picks = {
      "40m": "2026-09-05T12:00:00Z",
      "80m": "2026-09-07T12:00:00Z",
      "20m": "2026-09-06T12:00:00Z",
    };

    expect(recentlyPicked(sections, picks, 4).map((s) => s.id)).toEqual(["80m", "20m", "40m"]);
  });

  it("forgets a band the table no longer has", () => {
    // A renamed or dropped section would otherwise render a row with no band behind it.
    const picks = { "40m": "2026-09-07T12:00:00Z", "gone-band": "2026-09-07T13:00:00Z" };

    expect(recentlyPicked(sections, picks, 4).map((s) => s.id)).toEqual(["40m"]);
  });

  it("shows nothing at all before anything has been picked", () => {
    expect(recentlyPicked(sections, {}, 4)).toEqual([]);
  });

  it("stops at the limit", () => {
    const picks = {
      "40m": "2026-09-04T12:00:00Z",
      "80m": "2026-09-05T12:00:00Z",
      "20m": "2026-09-06T12:00:00Z",
      cb: "2026-09-07T12:00:00Z",
    };

    expect(recentlyPicked(sections, picks, 2).map((s) => s.id)).toEqual(["cb", "20m"]);
  });
});
