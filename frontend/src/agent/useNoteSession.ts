// Which conversation a note has. The notes list is Entry's session picker (the owner's
// ruling of 2026-09-14: *"the default view of entry would be notes. And when you select a
// note, it basically loads a conversation the same as if I had swiped left inside of jerv
// and picked a different conversation"*), and a note names a note — not a session id. This
// is the one hop between them: `GET /notes/{id}/thread`, which answers null for a note the
// box has not read yet rather than 404ing (see `api/notes.py`).

import { useEffect, useState } from "react";
import { type NoteThreadOut, api } from "../api/client";

export interface NoteSession {
  /** The note's conversation, once known. */
  sessionId: string | null;
  /** Whether the lookup has answered. Distinguishes "no conversation" from "not looked
   * yet", so the surface never flashes "the box hasn't read this note" at a note whose
   * thread is one tick away. */
  looked: boolean;
}

export function useNoteSession(
  noteId: string | null,
  lookup: (noteId: string) => Promise<NoteThreadOut | null> = api.noteThread,
): NoteSession {
  const [state, setState] = useState<NoteSession>({ sessionId: null, looked: false });

  useEffect(() => {
    if (noteId === null) {
      setState({ sessionId: null, looked: false });
      return;
    }
    // Blank first: the answer for the PREVIOUS note must never be read as this one's.
    setState({ sessionId: null, looked: false });
    let stale = false;
    void lookup(noteId)
      .then((found) => {
        if (!stale) setState({ sessionId: found?.session_id ?? null, looked: true });
      })
      .catch(() => {
        // A note with no thread is the ordinary case this answers null for, and a failed
        // lookup is indistinguishable from it as far as this hook can tell.
        if (!stale) setState({ sessionId: null, looked: true });
      });
    return () => {
      stale = true;
    };
  }, [noteId, lookup]);

  return state;
}
