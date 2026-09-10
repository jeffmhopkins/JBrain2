// The trim sheet against its binding spec (docs/mocks/recording/d-trim-sheet.html).
//
// This is the app's first irreversible edit of stored content, and every property pinned
// here is one of DESIGN.md's "Destructive editing of stored media" rules made testable.
// The trim DISCARDS THE ORIGINAL AND THERE IS NO UNDO, so a redesign that quietly lost
// Preview, or softened the confirm's wording, or started offering to "trim" a selection
// that frees nothing, would each turn a careful surface into a destructive one.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type SdrRecording, api } from "../api/client";
import { FRAME_S } from "../sdrTrim";
import { SdrTrimSheet } from "./SdrTrimSheet";

/** The mock's 3:06 weather clip: its useful twenty seconds cost 1.4 MB to keep whole. */
const CLIP: SdrRecording = {
  id: "wx",
  started_at: "2026-09-10T19:12:00Z",
  ended_at: "2026-09-10T19:15:06Z",
  duration_s: 186,
  captured_s: 186,
  frequency_hz: 162_550_000,
  mode: "fm",
  bandwidth_hz: 16_000,
  bytes: 186 * 8000,
  peaks: [0.04, 0.05, 0.9, 0.8, 0.04, 0.03],
  transcript: null,
  transcribed_at: null,
};

/** The same clip as the LIBRARY hands it over: no waveform.
 *
 *  Built by omitting the key rather than setting it undefined — `exactOptionalPropertyTypes`
 *  makes those different types, and the list genuinely omits it. */
const { peaks: _omitted, ...FROM_LIST } = CLIP;

function open(over: Partial<SdrRecording> = {}, base: SdrRecording = CLIP) {
  const onClose = vi.fn();
  const onTrimmed = vi.fn();
  render(<SdrTrimSheet recording={{ ...base, ...over }} onClose={onClose} onTrimmed={onTrimmed} />);
  return { onClose, onTrimmed };
}

const endHandle = () => screen.getByRole("slider", { name: "End of the trim" });
const startHandle = () => screen.getByRole("slider", { name: "Start of the trim" });
const confirm = () => screen.getByRole("button", { name: /Trim & discard rest/ });

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("where the waveform comes from", () => {
  it("fetches the envelope by id when the row arrived without one", async () => {
    // The library omits `peaks` on purpose — 400 floats a row would dwarf a hundred-row
    // response — so this fetch is the ONLY way the sheet ever gets a picture on a real
    // box. Without it the sheet is two handles over nothing, which is the shape's whole
    // argument missing: a cut is placeable because the dead air at each end is visible.
    const asked = vi
      .spyOn(api, "getSdrRecording")
      .mockResolvedValue({ ...CLIP, peaks: [0.02, 0.95, 0.02] });

    open({}, FROM_LIST);

    await waitFor(() => expect(asked).toHaveBeenCalledWith("wx"));
    await waitFor(() => expect(screen.getByText("drag either handle")).toBeInTheDocument());
  });

  it("does not ask again when the row already carries one", () => {
    // A row that came from a trim response has its new envelope already. Re-fetching
    // would repaint the picture the owner is mid-drag on.
    const asked = vi.spyOn(api, "getSdrRecording");

    open();

    expect(asked).not.toHaveBeenCalled();
  });

  it("stays usable when the envelope cannot be read", async () => {
    // A box older than the by-id route, or one whose decode failed. The handles, the
    // readout and the confirm all still work, so failing loudly here would block a trim
    // the owner can still make — the marks row says what is missing instead.
    vi.spyOn(api, "getSdrRecording").mockRejectedValue(new Error("nope"));

    open({}, FROM_LIST);

    await waitFor(() => expect(screen.getByText("no waveform stored")).toBeInTheDocument());
    expect(confirm()).toBeInTheDocument();
  });
});

describe("the trim sheet", () => {
  it("is the shared sheet shell, not a modal of its own", () => {
    // A bespoke modal is a design-doc violation, and the shell is what gives this the
    // Escape / swipe-down / Back-gesture dismissals every other sheet in the app has.
    open();
    expect(screen.getByRole("dialog", { name: "Trim 162.550 MHz" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Close" })).toBeTruthy();
  });

  it("opens on the whole clip and offers no saving until one is selected", () => {
    // A trim that keeps everything deletes one blob and writes an identical one: it
    // costs the original and frees nothing, which inverts the feature.
    open();
    expect(document.querySelector(".trim-gain")?.textContent).toContain("Nothing trimmed yet");
    expect(confirm()).toBeDisabled();
  });

  it("draws the selection with two handles bounded by the clip", () => {
    // Drawn, not typed: a selection over a continuous medium is placed on the picture of
    // it, and both handles are real sliders so a keyboard reaches them.
    open();
    expect(startHandle().getAttribute("aria-valuemax")).toBe("186");
    expect(endHandle().getAttribute("aria-valuenow")).toBe("186");
    expect(startHandle().getAttribute("aria-valuetext")).toBe("0:00.0");
  });

  it("moves a handle one frame at a time, and about a second with Shift", () => {
    // 72 ms is the smallest cut `-c copy` can make, so it is the arrow key's step. Shift
    // exists because frame-by-frame across three minutes is two thousand presses.
    const value = () => Number(endHandle().getAttribute("aria-valuenow"));
    open();

    // The first press leaves the clip's own end, which is the one position NOT on the
    // frame grid — so it lands on the grid, within a frame either side of the step.
    fireEvent.keyDown(endHandle(), { key: "ArrowLeft" });
    const first = value();
    expect(186 - first).toBeGreaterThan(0);
    expect(186 - first).toBeLessThanOrEqual(2 * FRAME_S);

    // From there every press is exactly one frame, which is the property that matters:
    // the nudge buttons and the arrows are the same gesture at the same resolution.
    fireEvent.keyDown(endHandle(), { key: "ArrowLeft" });
    expect(first - value()).toBeCloseTo(FRAME_S, 2);

    fireEvent.keyDown(endHandle(), { key: "ArrowLeft", shiftKey: true });
    expect(first - value()).toBeCloseTo(1 + FRAME_S, 1);
  });

  it("names the frame in the nudge buttons rather than implying finer precision", () => {
    // The UI must not promise milliseconds the format cannot deliver.
    open();
    expect(screen.getByRole("button", { name: "−72 ms in" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "+72 ms in" }));
    expect(Number(startHandle().getAttribute("aria-valuenow"))).toBeCloseTo(FRAME_S, 2);
  });

  it("states the saving live, in bytes, as the handles move", () => {
    // Rule 5: the whole reason the feature exists is disk, and the owner runs this box
    // remotely — they cannot go and look at it.
    // Ten coarse presses walk the start in by ten frame-aligned seconds — 10.08 s — and
    // at the sidecar's 8 kB/s that is 80,640 bytes, which is 79 kB. Change the step, the
    // grid or the rate and this number moves.
    open();
    for (let press = 0; press < 10; press++) {
      fireEvent.keyDown(startHandle(), { key: "ArrowRight", shiftKey: true });
    }
    const line = document.querySelector(".trim-gain")?.textContent ?? "";
    expect(line).toContain("Discards 0:10 of dead air");
    expect(line).toContain("frees 79 kB");
  });

  it("names the loss on the confirm instead of calling it Save", () => {
    // Rule 3. Arm-then-confirm is for actions with no preview; a sheet that HAS one has
    // already done that work, so the wording is what carries the warning.
    open();
    expect(confirm().textContent).toBe("Trim & discard rest");
    expect(document.querySelector(".trim-foot")?.textContent).toContain(
      "The full capture is discarded",
    );
  });

  it("previews only the selection, which is why the sheet is worth its cost", async () => {
    // Rule 2: when the original will not survive, the sheet must be able to play exactly
    // what will remain. Playback starts at the handle, not at zero.
    open();
    fireEvent.keyDown(startHandle(), { key: "ArrowRight", shiftKey: true });
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));

    const element = document.querySelector("audio") as HTMLAudioElement;
    expect(element.getAttribute("src")).toBe("/api/sdr/recordings/wx/audio");
    expect(element.currentTime).toBeCloseTo(1, 1);
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy());
  });

  it("sends the handles' seconds and takes the row the server actually cut", async () => {
    // The client asks in seconds; the copy lands on a frame boundary, so what comes back
    // is the truth and replaces the row wholesale rather than being merged with a guess.
    const cut = {
      recording: { ...CLIP, duration_s: 20.016, captured_s: 186, bytes: 160_128 },
      cut: { start_s: 0, end_s: 20.016 },
      usage: { bytes: 160_128, count: 1, reclaimed_bytes: 1_327_872 },
    };
    const trim = vi.spyOn(api, "trimSdrRecording").mockResolvedValue(cut);
    const { onTrimmed, onClose } = open();

    fireEvent.keyDown(endHandle(), { key: "ArrowLeft", shiftKey: true });
    fireEvent.click(confirm());

    // Within a frame of 185 s: the coarse step lands on the grid, and the server is the
    // one that decides exactly where the copy cuts.
    await waitFor(() => expect(trim).toHaveBeenCalledWith("wx", 0, expect.closeTo(185, 1)));
    expect(onTrimmed).toHaveBeenCalledWith(cut);
    expect(onClose).toHaveBeenCalled();
  });

  it("keeps the sheet open and says why when the box refuses the cut", async () => {
    // Closing on a failure would leave the owner believing a destructive edit landed.
    vi.spyOn(api, "trimSdrRecording").mockRejectedValue(new Error("ffmpeg could not read it"));
    const { onClose } = open();

    fireEvent.keyDown(endHandle(), { key: "ArrowLeft", shiftKey: true });
    fireEvent.click(confirm());

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("ffmpeg could not read it"),
    );
    expect(onClose).not.toHaveBeenCalled();
  });

  it("stops the preview at the out-point instead of running on to the end", async () => {
    // "Plays only the selection" is half the rule; the other half is that it STOPS
    // there. A preview that ran past the out-point would play the owner exactly the
    // audio the confirm is about to destroy, and call it what will remain.
    open();
    fireEvent.keyDown(endHandle(), { key: "ArrowLeft", shiftKey: true });
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy());

    const element = document.querySelector("audio") as HTMLAudioElement;
    const pause = vi.spyOn(element, "pause");
    element.currentTime = 185.5;
    fireEvent.timeUpdate(element);

    expect(pause).toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Preview" })).toBeTruthy());
    expect(document.querySelector(".trim-head")).toBeNull();
  });

  it("offers no way to delete the whole recording from here", async () => {
    // Delete belongs on the library row (a-tape-deck.html), not in the sheet whose
    // purpose is a DIFFERENT irreversible act: a button that discards everything sitting
    // inches from "Trim & discard rest" is one slip from the wrong loss.
    const remove = vi.spyOn(api, "deleteSdrRecording");
    open();
    expect(screen.queryByRole("button", { name: /Delete/i })).toBeNull();
    expect(remove).not.toHaveBeenCalled();
  });
});
