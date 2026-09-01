// The shared radio reading. What matters is that every reader sees ONE answer —
// the composer icon and the tuner sheet must never disagree about whether the
// radio is held — and that a box with no radio never lights the icon.

import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api/client";
import { resetSdrSession, subscribeSdr } from "./sdrSession";

const LISTENING = {
  session_id: "abc123",
  frequency_hz: 99_300_000,
  mode: "wbfm",
  gain: null,
  elapsed_s: 4,
  peak: 0.42,
  listeners: 1,
};

afterEach(() => {
  resetSdrSession();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

async function settle() {
  await vi.advanceTimersByTimeAsync(0);
}

describe("the shared radio reading", () => {
  it("publishes a live session to its subscriber", async () => {
    vi.useFakeTimers();
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: LISTENING });
    const seen: unknown[] = [];

    subscribeSdr((state) => seen.push(state));
    await settle();

    expect(seen.at(-1)).toEqual({ available: true, listening: LISTENING });
  });

  it("serves every reader from one poll", async () => {
    vi.useFakeTimers();
    const status = vi
      .spyOn(api, "getSdrStatus")
      .mockResolvedValue({ available: true, listening: LISTENING });

    subscribeSdr(() => {});
    subscribeSdr(() => {});
    await settle();

    // Two readers, one request — this is what keeps the icon and the sheet in
    // agreement rather than racing two independent fetches.
    expect(status).toHaveBeenCalledTimes(1);
  });

  it("gives a late joiner the current reading rather than a blank", async () => {
    vi.useFakeTimers();
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: LISTENING });
    subscribeSdr(() => {});
    await settle();

    const late: unknown[] = [];
    subscribeSdr((state) => late.push(state));

    expect(late[0]).toEqual({ available: true, listening: LISTENING });
  });

  it("stops polling when the last reader leaves", async () => {
    vi.useFakeTimers();
    const status = vi
      .spyOn(api, "getSdrStatus")
      .mockResolvedValue({ available: false, listening: null });

    const off = subscribeSdr(() => {});
    await settle();
    off();
    await vi.advanceTimersByTimeAsync(5000);

    expect(status).toHaveBeenCalledTimes(1); // no polling after the last unsubscribe
  });

  it("reports idle when the principal may not see the radio", async () => {
    vi.useFakeTimers();
    vi.spyOn(api, "getSdrStatus").mockRejectedValue(new ApiError(403, "nope"));
    const seen: { listening: unknown }[] = [];

    subscribeSdr((state) => seen.push(state));
    await settle();

    // A radio we cannot see is one we must stop claiming — a lit icon over a
    // radio the session can't reach is worse than no icon.
    expect(seen.at(-1)?.listening).toBeNull();
  });

  it("keeps the last good reading through a transient failure", async () => {
    vi.useFakeTimers();
    const status = vi.spyOn(api, "getSdrStatus");
    status.mockResolvedValueOnce({ available: true, listening: LISTENING });
    const seen: { listening: unknown }[] = [];
    subscribeSdr((state) => seen.push(state));
    await settle();

    status.mockRejectedValueOnce(new Error("network blip"));
    await vi.advanceTimersByTimeAsync(1000);

    // A blip must not blink the icon off and back on.
    expect(seen.at(-1)?.listening).toEqual(LISTENING);
  });
});
