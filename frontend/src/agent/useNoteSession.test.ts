// The note -> conversation hop, and the state it used to throw away.
//
// `GET /notes/{id}/thread` has always returned the thread's state; the hook returned only
// the session id, so Entry showed "No conversation yet — the box reads a note once it has
// indexed it" for the whole of the first pass, while the pass was running. The owner
// reported it as "when first analysing the note, I can't see the thinking trace". These
// cases pin the two states that are not "settled", because they are the ones the surface
// was blind to.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NoteThreadOut } from "../api/client";
import { useNoteSession } from "./useNoteSession";

function thread(state: string): NoteThreadOut {
  return { session_id: "s1", agent: "note_ingest", state };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("which conversation a note has", () => {
  it("reports a running pass as analysing, not as 'no conversation'", async () => {
    const lookup = vi.fn(async () => thread("running"));
    const { result } = renderHook(() => useNoteSession("n1", lookup));
    await waitFor(() => expect(result.current.looked).toBe(true));
    expect(result.current.analysing).toBe(true);
    expect(result.current.sessionId).toBe("s1");
  });

  it("a settled thread is not analysing and is not polled", async () => {
    const lookup = vi.fn(async () => thread("settled"));
    const { result } = renderHook(() => useNoteSession("n1", lookup));
    await waitFor(() => expect(result.current.looked).toBe(true));
    expect(result.current.analysing).toBe(false);
    expect(result.current.pending).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    // One read, ever: a finished thread does not change on its own.
    expect(lookup).toHaveBeenCalledTimes(1);
  });

  it("polls a running pass until it lands, then stops", async () => {
    let state = "running";
    const lookup = vi.fn(async () => thread(state));
    const { result } = renderHook(() => useNoteSession("n1", lookup));
    await waitFor(() => expect(result.current.analysing).toBe(true));
    const during = lookup.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_100);
    });
    expect(lookup.mock.calls.length).toBeGreaterThan(during);

    state = "settled";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_100);
    });
    await waitFor(() => expect(result.current.analysing).toBe(false));

    const settled = lookup.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(lookup).toHaveBeenCalledTimes(settled);
  });

  it("watches a note that has no thread YET — the half he sees first", async () => {
    // He writes a note and looks at it. The conversation does not exist until the note is
    // ingested, so without this the screen settles on "no conversation" and stays there
    // until he navigates away and back.
    let found: NoteThreadOut | null = null;
    const lookup = vi.fn(async () => found);
    const { result } = renderHook(() => useNoteSession("n1", lookup));
    await waitFor(() => expect(result.current.pending).toBe(true));

    found = thread("running");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_100);
    });
    await waitFor(() => expect(result.current.analysing).toBe(true));
  });

  it("a failed lookup never claims a pass is running", async () => {
    // A spinner that resolves only when the server recovers is worse than the plain
    // "no conversation" this degrades to.
    const lookup = vi.fn(async () => {
      throw new Error("offline");
    });
    const { result } = renderHook(() => useNoteSession("n1", lookup));
    await waitFor(() => expect(result.current.looked).toBe(true));
    expect(result.current.analysing).toBe(false);
    expect(result.current.sessionId).toBeNull();
  });

  it("blanks between notes, so one note's answer is never read as another's", async () => {
    const lookup = vi.fn(async (id: string) =>
      id === "n1" ? thread("settled") : thread("running"),
    );
    const { result, rerender } = renderHook(({ id }) => useNoteSession(id, lookup), {
      initialProps: { id: "n1" },
    });
    await waitFor(() => expect(result.current.sessionId).toBe("s1"));
    rerender({ id: "n2" });
    await waitFor(() => expect(result.current.analysing).toBe(true));
  });
});
