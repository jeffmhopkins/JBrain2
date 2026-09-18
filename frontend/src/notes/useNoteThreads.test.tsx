// The stream chip's data, and the two ways it went blank (R3f's review, findings 8).
//
// The chip is the ONLY route from the stream to a waiting note's thread (§3b I2, decided
// (ii)), so a hook that drops what it knows drops the owner's question with it.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type NotesInboxRow, api } from "../api/client";
import { useNoteThreads } from "./useNoteThreads";

function waiting(over: Partial<NotesInboxRow> = {}): NotesInboxRow {
  return {
    kind: "question",
    session_id: "s1",
    agent: "note_ingest",
    note_id: "n1",
    domain: "health",
    quote: "Kaiya started the new med…",
    asks: ["Which Dr. Chen?", "What's the medication called?"],
    captured_at: null,
    waiting_since: "2026-09-09T21:20:00Z",
    committed: 0,
    live: false,
    ...over,
  };
}

beforeEach(() => {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
});
afterEach(() => vi.restoreAllMocks());

describe("useNoteThreads", () => {
  it("chips a waiting thread with its open question count", async () => {
    vi.spyOn(api, "notesInbox").mockResolvedValue({ items: [waiting()] });
    const { result } = renderHook(() => useNoteThreads(true));
    await waitFor(() => expect(result.current.size).toBe(1));
    expect(result.current.get("n1")).toEqual({
      sessionId: "s1",
      agent: "note_ingest",
      questions: 2,
    });
  });

  // One transient 500 used to blank every chip on the stream for up to a poll — and with
  // the chip gone, so is the only way in to the question.
  it("keeps the last good value when the route fails", async () => {
    const inbox = vi
      .spyOn(api, "notesInbox")
      .mockResolvedValueOnce({ items: [waiting()] })
      .mockRejectedValueOnce(new Error("502"));
    const { result } = renderHook(() => useNoteThreads(true));
    await waitFor(() => expect(result.current.size).toBe(1));

    await act(async () => {
      await inbox.mock.results[0]?.value;
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => expect(inbox).toHaveBeenCalledTimes(2));
    expect(result.current.get("n1")?.questions).toBe(2);
  });

  // The poll, the foreground catch-up and the mount tick can be in flight at once and
  // resolve in any order; a slow early load landing last put stale chips back on screen.
  it("ignores a load that resolves after a newer one", async () => {
    let releaseFirst: ((v: { items: NotesInboxRow[] }) => void) | undefined;
    const first = new Promise<{ items: NotesInboxRow[] }>((r) => {
      releaseFirst = r;
    });
    const inbox = vi
      .spyOn(api, "notesInbox")
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce({ items: [] });

    const { result } = renderHook(() => useNoteThreads(true));
    await waitFor(() => expect(inbox).toHaveBeenCalledTimes(1));
    // A second load starts and finishes first: the thread has settled, no chip.
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => expect(inbox).toHaveBeenCalledTimes(2));
    // Now the stale one lands.
    await act(async () => {
      releaseFirst?.({ items: [waiting()] });
      await first;
    });
    expect(result.current.size).toBe(0);
  });
});
