import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type NoteOut, api } from "../api/client";
import { type OutboxStore, type PendingNote, createMemoryStore } from "./outbox";
import { useNotes } from "./useNotes";

const IDLE_INTERVAL_MS = 30_000;

function pendingNote(clientId: string): PendingNote {
  return {
    client_id: clientId,
    domain: "general",
    destination: null,
    body: "queued",
    created_at: new Date().toISOString(),
    attachments: [],
  };
}

async function storeWith(clientId: string): Promise<OutboxStore> {
  const store = createMemoryStore();
  await store.put(pendingNote(clientId));
  return store;
}

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

describe("useNotes outbox flushing across screens", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility("visible");
    vi.spyOn(api, "listNotes").mockResolvedValue({ notes: [], next_cursor: null });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  const flush = () => act(async () => void (await vi.advanceTimersByTimeAsync(0)));

  it("flushes the outbox on reconnect even while the stream is off-screen", async () => {
    const create = vi.spyOn(api, "createNote").mockResolvedValue({ id: "n1" } as NoteOut);
    const store = await storeWith("c1");
    // Off-screen: the list poll never runs, so nothing flushes on its own.
    renderHook(() => useNotes(true, false, store));
    await flush();
    expect(create).not.toHaveBeenCalled();

    // A reconnect from any screen drains the queue.
    act(() => void window.dispatchEvent(new Event("online")));
    await flush();
    expect(create).toHaveBeenCalledTimes(1);
  });

  it("ignores a reconnect entirely while the PWA is hidden", async () => {
    setVisibility("hidden");
    const create = vi.spyOn(api, "createNote").mockResolvedValue({ id: "n1" } as NoteOut);
    const store = await storeWith("c1");
    renderHook(() => useNotes(true, false, store));
    await flush();

    act(() => void window.dispatchEvent(new Event("online")));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_INTERVAL_MS * 2);
    });
    expect(create).not.toHaveBeenCalled();
    expect(api.listNotes).not.toHaveBeenCalled();
  });

  it("retries a transiently-failed flush off-screen, flush-only, until it drains", async () => {
    let failNext = true;
    const create = vi.spyOn(api, "createNote").mockImplementation(async () => {
      if (failNext) throw new Error("transient 500");
      return { id: "n1" } as NoteOut;
    });
    const store = await storeWith("c1");

    // Visible at first: the mount sync attempts a flush (fails) and populates the
    // pending row, then one list read.
    const { rerender } = renderHook(({ visible }) => useNotes(true, visible, store), {
      initialProps: { visible: true },
    });
    await flush();
    expect(create).toHaveBeenCalledTimes(1);
    expect(api.listNotes).toHaveBeenCalledTimes(1);

    // Leave the stream: the list poll stops, but the flush-only retry stays armed
    // because a note is still pending. The next tick (now succeeding) drains it…
    failNext = false;
    rerender({ visible: false });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_INTERVAL_MS);
    });
    expect(create).toHaveBeenCalledTimes(2);
    // …without ever issuing another list read while off-screen.
    expect(api.listNotes).toHaveBeenCalledTimes(1);

    // Drained: the retry disarms, so further ticks do nothing.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_INTERVAL_MS * 3);
    });
    expect(create).toHaveBeenCalledTimes(2);
  });
});
