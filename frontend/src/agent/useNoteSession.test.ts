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

let moved = 0;

/** A thread reading. `updated_at` advances on every build, because the server bumps it on
 * every state change — a fixture that held it still would let a stale-stamp bug pass. */
function thread(state: string): NoteThreadOut {
  moved += 1;
  return {
    session_id: "s1",
    agent: "note_ingest",
    state,
    updated_at: new Date(Date.UTC(2026, 8, 15, 0, 0, moved)).toISOString(),
  };
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

  it("keeps watching a SETTLED thread, because it is not finished being written to", async () => {
    // ⟲ This used to assert one read ever — "a finished thread does not change on its
    // own" — and that is false for a note. The owner replies, the append re-ingests the
    // note, and the re-reading lands in this same thread from the worker with nothing
    // streaming to this client. He reported it: the conversation only caught up "after I
    // had left the conversation and went back into it". Slower than a live pass, and not
    // zero.
    const lookup = vi.fn(async () => thread("settled"));
    const { result } = renderHook(() => useNoteSession("n1", lookup));
    await waitFor(() => expect(result.current.looked).toBe(true));
    expect(result.current.analysing).toBe(false);
    expect(result.current.pending).toBe(false);

    const opened = lookup.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(11_000);
    });
    const spent = lookup.mock.calls.length - opened;
    // Two ticks in eleven seconds — the settled cadence, not the live one.
    expect(spent).toBeGreaterThanOrEqual(1);
    expect(spent).toBeLessThanOrEqual(3);
  });

  it("reports a NEW movement stamp, which is what tells a screen to re-read the thread", async () => {
    // `state` alone cannot carry this: a re-reading moves settled → running → settled, so
    // a poll landing either side of it reads `settled` both times. Only `updated_at`
    // separates "nothing happened" from "a whole pass happened while you watched".
    const lookup = vi.fn(async () => thread("settled"));
    const { result } = renderHook(() => useNoteSession("n1", lookup));
    await waitFor(() => expect(result.current.movedAt).not.toBe(""));
    const first = result.current.movedAt;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(11_000);
    });
    expect(result.current.movedAt).not.toBe(first);
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

    // It drops to the settled cadence rather than stopping — see the case above.
    const settled = lookup.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    expect(lookup.mock.calls.length - settled).toBeLessThanOrEqual(1);
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

  it("a dropped request does not end the watch or unsay what it knows", async () => {
    // The regression this hook shipped and a review caught: the failure arm blanked
    // every field, so `analysing` went false, the interval was cleared and never
    // re-armed, and the screen fell back to "No conversation yet" over a LIVE thread —
    // permanently, on one dropped request out of a poll running twice a second.
    let fail = false;
    const lookup = vi.fn(async () => {
      if (fail) throw new Error("radio dropped");
      return thread("running");
    });
    const { result } = renderHook(() => useNoteSession("n1", lookup));
    await waitFor(() => expect(result.current.analysing).toBe(true));

    fail = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_100);
    });
    expect(result.current.analysing).toBe(true);
    expect(result.current.sessionId).toBe("s1");

    // And the poll is still alive, so it recovers on its own.
    fail = false;
    const before = lookup.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_100);
    });
    expect(lookup.mock.calls.length).toBeGreaterThan(before);
  });

  it("gives up watching a note whose thread never arrives", async () => {
    // A note can legitimately never get one — the worker quiesced by Ops → Update, a
    // dropped `note.ingested` — and an unbounded watch polls it every two seconds for as
    // long as the screen is open.
    const lookup = vi.fn(async () => null);
    const { result } = renderHook(() => useNoteSession("n1", lookup));
    await waitFor(() => expect(result.current.pending).toBe(true));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(130_000);
    });
    const spent = lookup.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(lookup).toHaveBeenCalledTimes(spent);
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
