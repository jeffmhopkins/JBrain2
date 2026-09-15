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
          analysing: threadState === "running",
          pending: threadState === null,
        });
      } catch {
        // A note with no thread is the ordinary case this answers null for, and a failed
        // lookup is indistinguishable from it as far as this hook can tell. Keep
        // `analysing` false: claiming a pass is running on the strength of a failed read
        // would show a spinner that never resolves.
        if (alive()) setState({ ...BLANK, looked: true });
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

  // The poll — while a pass is in flight, and while the note is still waiting for one. A
  // settled thread is not going to change on its own, and `document.hidden` means nobody
  // is watching it change. It runs only while this note's conversation is the open view,
  // so the cost is one request every two seconds for exactly as long as he is watching.
  const watching = state.analysing || state.pending;
  useEffect(() => {
    if (noteId === null || !watching) return;
    let stale = false;
    const alive = (): boolean => !stale;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void read(noteId, alive);
    }, RUNNING_POLL_MS);
    return () => {
      stale = true;
      window.clearInterval(timer);
    };
  }, [noteId, watching, read]);

  return state;
}
