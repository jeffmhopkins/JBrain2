// Which conversation a note has. The notes list is Entry's session picker (the owner's
// ruling of 2026-09-14: *"the default view of entry would be notes. And when you select a
// note, it basically loads a conversation the same as if I had swiped left inside of jerv
// and picked a different conversation"*), and a note names a note — not a session id. This
// is the one hop between them: `GET /notes/{id}/thread`, which answers null for a note the
// box has not read yet rather than 404ing (see `api/notes.py`).
//
// It also carries the thread's STATE, which the route has always returned and this hook
// used to drop on the floor. Dropping it is why the surface told the owner "No
// conversation yet — the box reads a note once it has indexed it" for the whole of the
// first pass: the box was reading the note at that moment, and the one word that said so
// arrived over the wire and was discarded. He reported it as "when first analysing the
// note, I can't see the thinking trace" — there was nothing to see because the screen
// denied the pass was happening.

import { useCallback, useEffect, useState } from "react";
import { type NoteThreadOut, api } from "../api/client";

/** How often to re-ask while a pass is in flight. The unattended pass is tens of seconds
 * (23s on the owner's box for a short note), so this is the resolution at which "it is
 * working" becomes "here is what it found" without polling being the expensive part. */
const RUNNING_POLL_MS = 2000;

/** How long to keep watching a note that has NO thread yet before giving up and showing
 * the static copy. A thread normally appears within a pass or two of the note being
 * ingested, but it can legitimately never arrive — the worker quiesced by Ops → Update, a
 * dropped `note.ingested`, a note older than note conversations — and an unbounded watch
 * would poll that note every two seconds for as long as the screen is open. A pass in
 * flight is NOT capped: that one is known to be running and known to end. */
const PENDING_WATCH_MS = 120_000;

/** How often to re-ask once the thread has SETTLED. Slower than a live pass, and not
 * zero: a settled note thread is not finished being written to. The owner replies, the
 * append re-ingests the note, and the re-reading lands in this same thread from the
 * worker — with nothing streaming to this client either time. Without this arm the
 * conversation only caught up when he left and came back, which is what he reported
 * about his reply as well as about the first pass. Visible-and-open only. */
const SETTLED_POLL_MS = 5000;

/** The thread states the route reports (`models/note_conversation.py`). `running` and
 * `waiting_on_owner` are the live pair; a pass is only IN FLIGHT on the first. */
export type NoteThreadState = "running" | "waiting_on_owner" | "settled" | "failed" | null;

export interface NoteSession {
  /** The note's conversation, once known. */
  sessionId: string | null;
  /** Whether the lookup has answered. Distinguishes "no conversation" from "not looked
   * yet", so the surface never flashes "the box hasn't read this note" at a note whose
   * thread is one tick away. */
  looked: boolean;
  /** The thread's state, or null when it has none. */
  state: NoteThreadState;
  /** When the thread last moved, as the server reports it — "" when it has none. The
   * screen watches THIS to know the transcript may have grown, because `state` alone
   * misses a pass that starts and finishes between two polls (settled → running →
   * settled reads `settled` both times). */
  movedAt: string;
  /** A pass is running on this note right now. The reason the state is carried at all:
   * it is the difference between "the box has not read this" and "the box is reading
   * this", and those are opposite things to tell someone waiting. */
  analysing: boolean;
  /** The note has been looked up and has no thread yet — a note captured seconds ago,
   * still queued behind its own ingest. Watched like `analysing` is, because this is the
   * half of the wait the owner sees FIRST: he writes a note and looks at it. */
  pending: boolean;
}

const BLANK: NoteSession = {
  sessionId: null,
  looked: false,
  state: null,
  movedAt: "",
  analysing: false,
  pending: false,
};

export function useNoteSession(
  noteId: string | null,
  lookup: (noteId: string) => Promise<NoteThreadOut | null> = api.noteThread,
): NoteSession {
  const [state, setState] = useState<NoteSession>(BLANK);

  const read = useCallback(
    async (id: string, alive: () => boolean): Promise<void> => {
      try {
        const found = await lookup(id);
        if (!alive()) return;
        const threadState = (found?.state ?? null) as NoteThreadState;
        setState({
          sessionId: found?.session_id ?? null,
          looked: true,
          state: threadState,
          movedAt: found?.updated_at ?? "",
          analysing: threadState === "running",
          pending: threadState === null,
        });
      } catch {
        // ADDITIVE, and that is the whole of it: a dropped request is not evidence that
        // the thread vanished. Blanking here (which this did) clobbered `sessionId` and
        // `analysing`, so `watching` went false, the interval was cleared and never
        // re-armed, and the screen fell back to "No conversation yet — the box reads a
        // note once it has indexed it" — the exact sentence this hook exists to stop —
        // over a live, streaming thread, permanently, on one dropped request out of a
        // poll running twice a second on a phone.
        if (alive()) setState((prev) => ({ ...prev, looked: true }));
      }
    },
    [lookup],
  );

  useEffect(() => {
    if (noteId === null) {
      setState(BLANK);
      return;
    }
    // Blank first: the answer for the PREVIOUS note must never be read as this one's.
    setState(BLANK);
    let stale = false;
    const alive = (): boolean => !stale;
    void read(noteId, alive);
    return () => {
      stale = true;
    };
  }, [noteId, read]);

  // The poll. It runs for as long as this note's conversation is the open view, because
  // every way this thread gains turns is one this client cannot watch: the unattended
  // pass runs in the worker, and so does the re-reading his own reply triggers. The
  // cadence says which of those is expected — fast while something is known to be in
  // flight, slow while the thread is merely open. `document.hidden` means nobody is
  // watching it change, so nothing is asked.
  const fast = state.analysing || state.pending;
  useEffect(() => {
    if (noteId === null || !state.looked) return;
    let stale = false;
    const alive = (): boolean => !stale;
    // The ceiling applies to the PENDING watch alone — a note whose thread never arrives
    // (the worker quiesced by Ops → Update, a dropped `note.ingested`) would otherwise be
    // asked about forever. A pass in flight is known to end, and a settled thread's slow
    // arm costs one request every five seconds for exactly as long as he is looking.
    const until = state.pending ? Date.now() + PENDING_WATCH_MS : Number.POSITIVE_INFINITY;
    const timer = window.setInterval(
      () => {
        if (Date.now() > until) {
          window.clearInterval(timer);
          return;
        }
        if (document.visibilityState === "hidden") return;
        void read(noteId, alive);
      },
      fast ? RUNNING_POLL_MS : SETTLED_POLL_MS,
    );
    return () => {
      stale = true;
      window.clearInterval(timer);
    };
  }, [noteId, state.looked, state.pending, fast, read]);

  return state;
}
