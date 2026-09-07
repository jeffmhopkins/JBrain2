// The band picker at 57 sections — shape C of docs/mocks/band-recents.
//
// What is worth pinning is the promise the shape makes: the list underneath is exactly
// the one that was always there, and the two additions (a filter, and what this device
// tuned lately) only ever put the right band nearer the top. A filter that hid a real
// band, or a Recent group that appeared on a device with no history, would be worse
// than the scrolling they replace.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { BAND_PICKS_KEY } from "../sdrBandPicks";
import { resetBands } from "../sdrBands";
import { SdrBandSheet, matching } from "./SdrBandSheet";

function section(over: Record<string, unknown> = {}) {
  return {
    id: "x",
    band: "B",
    name: "N",
    start_hz: 144_000_000,
    stop_hz: 145_000_000,
    mode: "fm",
    step_hz: 5_000,
    channel_hz: 25_000,
    note: "",
    live: "fast",
    continuous: false,
    sweep_seconds: 120,
    span_hz: 1_000_000,
    centre_hz: 144_500_000,
    hops: 1,
    duty: 1,
    surveyable: true,
    direct_sampling: false,
    sample_rate_hz: 1_024_000,
    fft_bins: 4_096,
    bin_hz: 250,
    image_start_hz: 0,
    image_stop_hz: 0,
    channels: [],
    ...over,
  };
}

const SECTIONS = [
  section({
    id: "fm-broadcast",
    band: "FM broadcast",
    name: "The dial",
    start_hz: 88_000_000,
    stop_hz: 108_000_000,
    note: "Commercial FM.",
  }),
  section({
    id: "air-centre",
    band: "Airband",
    name: "Centre",
    start_hz: 132_000_000,
    stop_hz: 134_000_000,
    mode: "am",
    note: "En-route control.",
  }),
  section({
    id: "2m-aprs",
    band: "2 m",
    name: "APRS",
    start_hz: 144_300_000,
    stop_hz: 145_100_000,
    note: "Position beacons.",
  }),
  section({
    id: "cb",
    band: "CB",
    name: "Citizens band",
    start_hz: 26_965_000,
    stop_hz: 27_405_000,
    mode: "am",
    note: "Forty channels.",
  }),
  section({
    id: "40m",
    band: "40 m",
    name: "Voice (LSB)",
    start_hz: 7_125_000,
    stop_hz: 7_300_000,
    mode: "lsb",
    direct_sampling: true,
    note: "The dependable band.",
  }),
];

function bands(sections = SECTIONS) {
  vi.spyOn(api, "getSdrBands").mockResolvedValue({
    region: "us",
    tuner_min_hz: 100_000,
    tuner_max_hz: 1_766_000_000,
    direct_max_hz: 24_000_000,
    sections,
  } as never);
}

function show(onPick = vi.fn()) {
  render(<SdrBandSheet purpose="listen" onPick={onPick} onClose={() => {}} />);
  return onPick;
}

/** The rows, in the order they are drawn — which is the whole subject here. */
function rowNames(): string[] {
  return screen
    .getAllByRole("button")
    .map((el) => el.textContent ?? "")
    .filter((text) => text.includes("·"));
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  resetBands();
  vi.restoreAllMocks();
});

describe("finding a band among 57", () => {
  it("filters on the band's own name", async () => {
    bands();
    show();

    fireEvent.change(await screen.findByLabelText("Filter bands"), {
      target: { value: "aprs" },
    });

    expect(await screen.findByText("1 of 5")).toBeInTheDocument();
    expect(rowNames().join(" ")).toContain("APRS");
    expect(rowNames().join(" ")).not.toContain("The dial");
  });

  it("filters on the GROUP, which is how a band is usually remembered", async () => {
    // Nobody looking for en-route control types "Centre" — they type "air".
    bands();
    show();

    fireEvent.change(await screen.findByLabelText("Filter bands"), {
      target: { value: "air" },
    });

    expect(await screen.findByText("1 of 5")).toBeInTheDocument();
    expect(rowNames().join(" ")).toContain("Centre");
  });

  it("filters on the frequency as it is written on the row", async () => {
    // "27" is how someone asks for CB. It is a match against the string the owner can
    // see, not a numeric comparison they would have to be exact about.
    bands();
    show();

    fireEvent.change(await screen.findByLabelText("Filter bands"), {
      target: { value: "27" },
    });

    expect(await screen.findByText("1 of 5")).toBeInTheDocument();
    expect(rowNames().join(" ")).toContain("Citizens band");
  });

  it("says so when nothing matches, and points at the way out", async () => {
    bands();
    show();

    fireEvent.change(await screen.findByLabelText("Filter bands"), {
      target: { value: "zzz" },
    });

    expect(await screen.findByText(/Nothing matches/)).toBeInTheDocument();
    // The expert path is still there — a filter that matched nothing must not look
    // like a radio that reaches nothing.
    expect(screen.getByText("Enter a frequency…")).toBeInTheDocument();
  });

  it("leaves the list exactly as it was while the field is empty", async () => {
    bands();
    show();

    await screen.findByText("FM broadcast");
    // Every group heading, in table order, and every row under it.
    expect(rowNames()).toHaveLength(SECTIONS.length);
    expect(screen.queryByText("Recent")).not.toBeInTheDocument();
  });
});

describe("what this device tuned lately", () => {
  it("leads the list once a band has been picked", async () => {
    bands();
    const onPick = show();

    fireEvent.click(await screen.findByText(/Voice \(LSB\)/));

    await waitFor(() => expect(onPick).toHaveBeenCalled());
    // The sheet stays mounted after a pick in this test, so the Recent group is drawn
    // from the store the pick just wrote.
    const recent = await screen.findByText("Recent");
    expect(recent).toBeInTheDocument();
    expect(rowNames()[0]).toContain("Voice (LSB)");
  });

  it("shows the most recent first, and only a few", async () => {
    localStorage.setItem(
      BAND_PICKS_KEY,
      JSON.stringify({
        "40m": "2026-09-05T12:00:00Z",
        cb: "2026-09-07T12:00:00Z",
        "2m-aprs": "2026-09-06T12:00:00Z",
      }),
    );
    bands();
    show();

    await screen.findByText("Recent");
    expect(rowNames().slice(0, 3).join(" | ")).toContain("Citizens band");
    expect(rowNames()[0]).toContain("Citizens band");
    expect(rowNames()[1]).toContain("APRS");
    expect(rowNames()[2]).toContain("Voice (LSB)");
  });

  it("still lists a recent band in its own group below", async () => {
    // The duplicate is the cost of shape A that C inherits, and it is deliberate: the
    // groups are how someone browses when they do not already know what they want.
    localStorage.setItem(BAND_PICKS_KEY, JSON.stringify({ cb: "2026-09-07T12:00:00Z" }));
    bands();
    show();

    await screen.findByText("Recent");
    expect(rowNames().filter((t) => t.includes("Citizens band"))).toHaveLength(2);
    expect(screen.getByText("CB")).toBeInTheDocument();
  });

  it("draws no Recent group on a device with no history", async () => {
    bands();
    show();

    await screen.findByText("FM broadcast");
    expect(screen.queryByText("Recent")).not.toBeInTheDocument();
  });

  it("opens the plain list when the history cannot be read", async () => {
    localStorage.setItem(BAND_PICKS_KEY, "{not json");
    bands();
    show();

    expect(await screen.findByText("FM broadcast")).toBeInTheDocument();
    expect(screen.queryByText("Recent")).not.toBeInTheDocument();
  });
});

describe("the matcher itself", () => {
  it("matches nothing away when the query is empty", () => {
    expect(matching(SECTIONS as never, "  ")).toHaveLength(SECTIONS.length);
  });

  it("ignores case", () => {
    expect(matching(SECTIONS as never, "CITIZENS").map((s) => s.id)).toEqual(["cb"]);
  });

  it("matches the note, so a band can be found by what it carries", () => {
    expect(matching(SECTIONS as never, "dependable").map((s) => s.id)).toEqual(["40m"]);
  });
});

describe("the sheet's own chrome", () => {
  it("keeps manual entry last, under the list rather than beside it", async () => {
    bands();
    show();

    const manual = await screen.findByText("Enter a frequency…");
    const list = manual.closest(".bandlist");
    expect(list).not.toBeNull();
    expect(
      within(list as HTMLElement)
        .getAllByRole("button")
        .at(-1)?.textContent,
    ).toContain("Enter a frequency…");
  });
});
