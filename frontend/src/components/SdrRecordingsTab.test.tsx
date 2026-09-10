// The recordings library (docs/mocks/recording/a-tape-deck.html + d-trim-sheet.html).
//
// What is pinned here is what the surface would quietly stop telling the owner if it were
// redesigned. This box is run REMOTELY with no terminal (CLAUDE.md #10), so a library
// that stops printing sizes, or a header that starts implying an expiry policy that does
// not exist, is a support call nobody can answer from a phone. And the two failures that
// look identical from the sofa — a box that answered with an error and a box that could
// not be reached at all — must both reach the screen rather than a spinner.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type SdrRecording, type SdrRecordingsPage, api } from "../api/client";
import type { OpsMetrics } from "../api/client";
import { resetSdrSession } from "../sdrSession";
import { SdrRecordingsTab } from "./SdrRecordingsTab";

const GB = 1024 * 1024 * 1024;

// Anchored at local noon rather than at Date.now(): the day headings are computed in
// local time, so a suite run just after midnight would file a clip made "an hour ago"
// under Yesterday and the grouping case would fail for the clock rather than the code.
const NOON = (() => {
  const at = new Date();
  at.setHours(12, 0, 0, 0);
  return at.getTime();
})();

function at(hoursAgo: number): string {
  return new Date(NOON - hoursAgo * 3_600_000).toISOString();
}

function row(over: Partial<SdrRecording> = {}): SdrRecording {
  const capturedS = over.captured_s ?? over.duration_s ?? 42;
  const durationS = over.duration_s ?? capturedS;
  return {
    id: "rec-1",
    started_at: at(1),
    ended_at: at(1),
    duration_s: durationS,
    captured_s: capturedS,
    frequency_hz: 5_000_000,
    mode: "am",
    bandwidth_hz: 6000,
    bytes: Math.round(durationS * 8000),
    peaks: [0.1, 0.8, 0.7, 0.1],
    transcript: { text: "At the tone, twenty-three hours forty-five minutes." },
    transcribed_at: at(1),
    ...over,
  };
}

function page(
  over: { recordings?: SdrRecording[]; usage?: Partial<SdrRecordingsPage["usage"]> } = {},
): SdrRecordingsPage {
  const recordings = over.recordings ?? [row()];
  return {
    recordings,
    usage: {
      bytes: recordings.reduce((total, r) => total + r.bytes, 0),
      count: recordings.length,
      reclaimed_bytes: 0,
      ...over.usage,
    },
  };
}

function library(value: SdrRecordingsPage = page()) {
  return vi.spyOn(api, "getSdrRecordings").mockResolvedValue(value);
}

beforeEach(() => {
  // The library reloads when a capture stops, off the shared 1 Hz poll — so the poll has
  // to answer or the tab never makes its first read.
  vi.spyOn(api, "getSdrStatus").mockResolvedValue({
    available: true,
    listening: null,
    sessions: [],
    recording: null,
  });
  // jsdom implements neither, and a recording is a stored FILE played through a real
  // media element (never sdrAudio.ts's one-shot live stream).
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});

afterEach(() => {
  resetSdrSession();
  vi.restoreAllMocks();
});

describe("the recordings library", () => {
  it("groups by day, newest first", async () => {
    library(
      page({
        recordings: [
          row({ id: "old", started_at: at(30), frequency_hz: 7_200_000 }),
          row({ id: "early", started_at: at(9), frequency_hz: 4_625_000 }),
          row({ id: "late", started_at: at(1), frequency_hz: 5_000_000 }),
        ],
      }),
    );
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() => expect(document.querySelectorAll(".rec-row").length).toBe(3));
    const days = [...document.querySelectorAll(".rec-day")].map((d) => d.textContent);
    expect(days[0]).toBe("Today");
    // One heading per day, and the newest clip heads the list under it.
    expect(new Set(days).size).toBe(days.length);
    expect(document.querySelector(".rec-row .rec-who b")?.textContent).toContain("5.000");
  });

  it("prints a size on every row, and meters the library against the real disk", async () => {
    // Sizes are always visible: the argument for trimming is disk, and a library that
    // quietly fills one is the failure this surface exists to prevent. The DENOMINATOR
    // is the host's, not the recordings document's — the api reports what the library
    // weighs, never what the box has.
    library(page({ recordings: [row({ duration_s: 186 })] }));
    vi.spyOn(api, "opsMetrics").mockResolvedValue({ disk_total_bytes: 8 * GB } as OpsMetrics);
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() => expect(document.querySelector(".rec-meta")?.textContent).toBeTruthy());
    expect(document.querySelector(".rec-meta")?.textContent).toContain("3:06");
    expect(document.querySelector(".rec-meta")?.textContent).toContain("1.4 MB");
    await waitFor(() =>
      expect(document.querySelector(".rec-disk")?.textContent).toContain("of 8.0 GB"),
    );
    expect(document.querySelector(".rec-bar")).toBeTruthy();
  });

  it("drops the bar rather than inventing a disk it could not read", async () => {
    // A meter against a made-up total is worse than a plain number, and the metrics
    // route is a separate permission from the radio's.
    library();
    vi.spyOn(api, "opsMetrics").mockRejectedValue(new ApiError(403, "not the owner"));
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() => expect(document.querySelector(".rec-disk")).toBeTruthy());
    expect(document.querySelector(".rec-disk")?.textContent).toContain("in 1 recording");
    expect(document.querySelector(".rec-bar")).toBeNull();
  });

  it("does not imply an expiry the box does not have", async () => {
    // NOTHING EXPIRES. There is no retention prune, so the resting header line says
    // these are kept until the owner removes them — and says what trimming reclaimed
    // only once trimming has actually reclaimed something.
    library();
    const { unmount } = render(<SdrRecordingsTab onOpenRadios={() => {}} />);
    await waitFor(() =>
      expect(document.querySelector(".rec-disk")?.textContent).toContain(
        "kept until you delete them",
      ),
    );
    unmount();

    library(page({ usage: { reclaimed_bytes: 509 * 1024 } }));
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);
    await waitFor(() =>
      expect(document.querySelector(".rec-freed")?.textContent).toBe(
        "509 kB reclaimed by trimming",
      ),
    );
  });

  it("says a row has already lost its original", async () => {
    // Derived from duration_s < captured_s rather than from a flag, so the badge cannot
    // drift away from the audio it describes.
    library(page({ recordings: [row({ duration_s: 10, captured_s: 27 })] }));
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() =>
      expect(document.querySelector(".rec-chip-cut")?.textContent).toBe("trimmed"),
    );
    expect(document.querySelector(".rec-chip")?.textContent).toBe("AM 6k");
  });

  it("points at the radio when there is nothing to list", async () => {
    // DESIGN.md's empty state: one sentence with the action inline, no illustration.
    // The action is not on this tab — Record lives on the radio — so it navigates there
    // rather than offering a control that could only fail.
    library(page({ recordings: [], usage: { bytes: 0, count: 0, reclaimed_bytes: 0 } }));
    const open = vi.fn();
    render(<SdrRecordingsTab onOpenRadios={open} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "a radio" })).toBeTruthy());
    expect(document.querySelector(".radio-empty")?.textContent).toContain("Nothing recorded yet");
    fireEvent.click(screen.getByRole("button", { name: "a radio" }));
    expect(open).toHaveBeenCalled();
  });

  it("shows the box's own refusal when the first read fails", async () => {
    // Not "Reading the library…" for ever: a first load that failed used to be the
    // DEFAULT experience on a box with no radio, on the neighbouring APRS tab.
    vi.spyOn(api, "getSdrRecordings").mockRejectedValue(
      new ApiError(500, "the recordings table is unreadable"),
    );
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("the recordings table is unreadable"),
    );
  });

  it("survives a failure that rejects rather than answering", async () => {
    // Offline: fetch REJECTS, it does not return a status. Anything that only handles
    // ApiError leaves this screen loading for ever.
    vi.spyOn(api, "getSdrRecordings").mockRejectedValue(new TypeError("Failed to fetch"));
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("Couldn't read the recordings library."),
    );
  });

  it("plays the STORED FILE, never the live stream", async () => {
    // sdrAudio.ts owns the one live <audio> for the life of the lease and its
    // createMediaElementSource is one-shot; a recording is a file, so it gets its own
    // element pointed at the by-id route Starlette serves Range on.
    library(page({ recordings: [row({ id: "abc" })] }));
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() => expect(document.querySelector(".rec-play")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Play the 5.000 MHz recording" }));

    const element = document.querySelector("audio") as HTMLAudioElement;
    expect(element.getAttribute("src")).toBe("/api/sdr/recordings/abc/audio");
    await waitFor(() => expect(document.querySelector(".rec-row-on")).toBeTruthy());
  });

  it("keeps play and expand as two separate controls", async () => {
    // The circle plays; the row opens its body. One control that chose between them by
    // where the tap landed would have a single accessible name for two actions, and
    // would make the body unreachable by keyboard.
    library(page({ recordings: [row({ id: "abc" })] }));
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() => expect(document.querySelector(".rec-main")).toBeTruthy());
    fireEvent.click(document.querySelector(".rec-main") as Element);

    expect(document.querySelector(".rec-body")).toBeTruthy();
    expect(document.querySelector(".rec-main")?.getAttribute("aria-expanded")).toBe("true");
    // Opening the body did not start playback.
    expect(document.querySelector(".rec-row-on")).toBeNull();
  });

  it("puts Download and Delete in the row's body, not in the trim sheet", async () => {
    // a-tape-deck.html is the library's spec and d-trim-sheet did not repeal it. Delete
    // must not live inside the sheet whose confirm is a DIFFERENT irreversible act.
    library(page({ recordings: [row({ id: "abc", started_at: at(1) })] }));
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() => expect(document.querySelector(".rec-main")).toBeTruthy());
    fireEvent.click(document.querySelector(".rec-main") as Element);

    const download = screen.getByRole("link", { name: /Download/ });
    // The blob's own URL handed to the browser — never a request through the client.
    expect(download.getAttribute("href")).toBe("/api/sdr/recordings/abc/audio");
    expect(download.getAttribute("download")).toMatch(/^5\.000MHz-.*\.mp3$/);
  });

  it("arms Delete before doing it, and disarms itself if walked away from", async () => {
    // The row stays on screen, so an armed delete that never times out is a loaded
    // control sitting in a scrolling list.
    library(page({ recordings: [row({ id: "abc" }), row({ id: "def", started_at: at(2) })] }));
    const remove = vi.spyOn(api, "deleteSdrRecording").mockResolvedValue({
      deleted: true,
      usage: { bytes: 0, count: 1, reclaimed_bytes: 0 },
    });
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() => expect(document.querySelectorAll(".rec-main").length).toBe(2));
    fireEvent.click(document.querySelectorAll(".rec-main")[0] as Element);
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(remove).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Tap again — deletes this recording/ }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("abc"));
    // The row goes, and the meter comes from the box's own answer rather than a guess.
    await waitFor(() => expect(document.querySelectorAll(".rec-row").length).toBe(1));
    expect(document.querySelector(".rec-disk")?.textContent).toContain("in 1 recording");
  });

  it("opens the trim sheet from the row's own action, not from the row", async () => {
    // An irreversible edit is entered deliberately. Tapping the row opens its body and
    // the circle plays it; only the 44px scissors column opens the trim.
    library(page({ recordings: [row({ frequency_hz: 146_940_000 })] }));
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() => expect(document.querySelector(".rec-trim")).toBeTruthy());
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Trim the 146.940 MHz recording" }));
    expect(screen.getByRole("dialog", { name: "Trim 146.940 MHz" })).toBeTruthy();
  });

  it("takes the trimmed row AND the meter from the server's own answer", async () => {
    // The cut lands on a frame boundary, so what a trim ACTUALLY freed is the box's to
    // report — the sheet's live figure was only ever an estimate of it, and the header's
    // meter must not go on printing an estimate. Both come back with the trim, which is
    // why the list does not re-read.
    const read = library(page({ recordings: [row({ id: "abc", duration_s: 42 })] }));
    vi.spyOn(api, "trimSdrRecording").mockResolvedValue({
      recording: row({ id: "abc", duration_s: 10.008, captured_s: 42, bytes: 80_064 }),
      cut: { start_s: 0, end_s: 10.008 },
      usage: { bytes: 80_064, count: 1, reclaimed_bytes: 255_936 },
    });
    render(<SdrRecordingsTab onOpenRadios={() => {}} />);

    await waitFor(() => expect(document.querySelector(".rec-trim")).toBeTruthy());
    fireEvent.click(document.querySelector(".rec-trim") as Element);
    // Pull the end in so there is something to discard — the confirm refuses a trim
    // that would free nothing.
    fireEvent.keyDown(screen.getByRole("slider", { name: "End of the trim" }), {
      key: "ArrowLeft",
      shiftKey: true,
    });
    fireEvent.click(screen.getByRole("button", { name: /Trim & discard rest/ }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.querySelector(".rec-freed")?.textContent).toBe("250 kB reclaimed by trimming");
    expect(document.querySelector(".rec-chip-cut")?.textContent).toBe("trimmed");
    expect(read).toHaveBeenCalledTimes(1);
  });
});
