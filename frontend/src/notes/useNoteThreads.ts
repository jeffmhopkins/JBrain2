// Which stream rows have a note conversation waiting on an answer (AGENT_INGEST_REWRITE
// §3b I1) — the chip's state, and the session it opens.
//
// The plan left the wire to the wave: either the notes list learns to carry a
// conversation's state and its open-question count, or the stream fetches the waiting set
// once and joins client-side. This takes the join, and the reason is that the route is
// ALREADY EXACTLY THIS SET. `/api/review/notes` returns every live note conversation,
// oldest wait first, with its note id, its session id, its persona and its whole open
// question set (`models/note_conversation.notes_inbox`) — the notes tab reads it, and D4's
// wire-level rule that a row is a REDIRECT and carries "no id or verb any answer could be
// posted against" is the same rule the chip needs to obey. Widening `NoteOut` would put a
// second, differently-shaped copy of that state on a route every note in the corpus pages
// through, for a set that is normally empty and never longer than a handful.
//
// What the join costs is one request beside the stream's own poll, and what it buys is
// that the chip and the notes tab cannot disagree about what is waiting.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { onForegroundSignals } from "../visibility";

/** A note whose thread is parked on the owner. */
export interface NoteThread {
  sessionId: string;
  /** The session's persona, so the redirect flips to the tab that hosts it first — a
   * handoff that lands on the wrong tab shows an empty chat, which reads as "the question
   * is gone". */
  agent: string;
  /** How many questions the open set holds — the chip's number. */
  questions: number;
}

/** Keyed by note id. Empty until the FIRST load resolves — a stream that silently shows
 * no chip is the shipped behaviour, where an error state on every row would be the
 * loudest thing on the screen. After that the last good value STANDS through a failure
 * (R3f's review, finding 8): blanking it on one transient 500 took every ask chip off the
 * stream for up to a poll, and the chip is the only route to the thread, so the owner's
 * question simply disappeared until it came back. A stale chip that opens a settled
 * thread is a far smaller wrong than a question with no door. */
export type NoteThreads = ReadonlyMap<string, NoteThread>;

const EMPTY: NoteThreads = new Map();
// Slower than the stream's active poll and faster than its idle one. A question appears
// when a pass ENDS, which is minutes of work, so this is about the chip being right when
// the owner next looks — not about it arriving the second it exists (D5: no nagging).
const POLL_MS = 20_000;

export function useNoteThreads(enabled: boolean): NoteThreads {
  const [threads, setThreads] = useState<NoteThreads>(EMPTY);
  // Which load is the newest. The poll, the foreground catch-up and the mount tick can
  // all be in flight at once, and they resolve in whatever order the network gives back;
  // without this a slow early load lands after a fast later one and the chips go stale.
  const generation = useRef(0);

  const load = useCallback(async () => {
    const mine = (generation.current += 1);
    try {
      const { items } = await api.notesInbox();
      const next = new Map<string, NoteThread>();
      for (const row of items) {
        // Only a thread genuinely WAITING earns a chip. A first pass still reading is
        // listed by the route (the notes tab shows it) but the stream already says so
        // through the lifecycle chip, and a staged approval is not about a note at all.
        if (row.kind !== "question" || row.live || row.note_id === null) continue;
        next.set(row.note_id, {
          sessionId: row.session_id,
          agent: row.agent,
          questions: row.asks.length,
        });
      }
      if (mine === generation.current) setThreads(next);
    } catch {
      // Keep the last good value. The next tick is 20 s away at worst, and the route
      // failing says nothing about whether a thread is still waiting.
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    const tick = () => {
      if (!stopped && document.visibilityState === "visible") void load();
    };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    // Catch up the moment the app comes back, rather than up to a poll late.
    const off = onForegroundSignals(tick);
    return () => {
      stopped = true;
      window.clearInterval(id);
      off();
    };
  }, [enabled, load]);

  return enabled ? threads : EMPTY;
}
