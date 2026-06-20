import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { createMemoryStore } from "./outbox";
import { useNotes } from "./useNotes";

const IDLE_INTERVAL_MS = 30_000;

function setVisibility(state: "visible" | "hidden") {
  act(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

describe("useNotes background suspension", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility("visible");
    vi.spyOn(api, "listNotes").mockResolvedValue({ notes: [], next_cursor: null });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  // Flush the sync's promise chain (flushOutbox → listNotes → setPending) under
  // fake timers; waitFor's real-timer polling would hang here.
  const flush = () => act(async () => void (await vi.advanceTimersByTimeAsync(0)));

  it("stops polling the server while backgrounded and catches up on return", async () => {
    const store = createMemoryStore();
    renderHook(() => useNotes(true, true, store));

    // The mount fires one immediate sync.
    await flush();
    expect(api.listNotes).toHaveBeenCalledTimes(1);

    // Backgrounding tears the interval down: ticking the clock sends nothing.
    setVisibility("hidden");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_INTERVAL_MS * 3);
    });
    expect(api.listNotes).toHaveBeenCalledTimes(1);

    // Returning to the foreground re-arms with an immediate catch-up sync.
    setVisibility("visible");
    await flush();
    expect(api.listNotes).toHaveBeenCalledTimes(2);
  });

  it("does not poll while the stream is off-screen, and resumes when shown", async () => {
    const store = createMemoryStore();
    // Mounted while another screen covers the stream: no immediate sync, no poll.
    const { rerender } = renderHook(({ visible }) => useNotes(true, visible, store), {
      initialProps: { visible: false },
    });
    await flush();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_INTERVAL_MS * 3);
    });
    expect(api.listNotes).not.toHaveBeenCalled();

    // Returning to the stream syncs at once, then keeps the list fresh.
    rerender({ visible: true });
    await flush();
    expect(api.listNotes).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_INTERVAL_MS);
    });
    expect(api.listNotes).toHaveBeenCalledTimes(2);
  });
});
