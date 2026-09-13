// The notes tab of the review inbox (D4/D5 of AGENT_INGEST_CONVERSATION_PLAN).
//
// A READ-ONLY controller, and that is the whole design: it exposes rows and a reload,
// and no resolve/approve/answer of any kind. The conversation is the only place note
// ingestion is decided — an answer affordance here would re-create the second decision
// surface the plan exists to delete — and the wire agrees (`NotesInboxRow` carries no
// item id and there is no endpoint to post one to), so this is enforced rather than
// merely intended.

import { useEffect, useState } from "react";
import { type NotesInboxRow, api } from "../api/client";

export interface NotesInboxController {
  /** null until the first load resolves. */
  rows: NotesInboxRow[] | null;
  loadError: boolean;
  /** Rows genuinely waiting on the owner — a first pass still reading is listed but
   * not counted (D5: no push, no nagging badge, and no zero to clear). */
  waitingCount: number;
}

export function useNotesInbox(): NotesInboxController {
  const [rows, setRows] = useState<NotesInboxRow[] | null>(null);
  const [loadError, setLoadError] = useState(false);

  // Loaded once per mount, and there is no refresh: the screen unmounts when the card
  // closes, so reopening the inbox is the reload. Nothing polls it — a question that
  // appears while the owner is looking at the list is D5's "no nagging".
  useEffect(() => {
    let stale = false;
    api
      .notesInbox()
      .then((q) => {
        if (!stale) setRows(q.items);
      })
      .catch(() => {
        if (!stale) setLoadError(true);
      });
    return () => {
      stale = true;
    };
  }, []);

  return { rows, loadError, waitingCount: (rows ?? []).filter((r) => !r.live).length };
}
