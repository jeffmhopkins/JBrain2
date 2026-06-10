// Note actions shared by the swipe rail and the note view: edit (loads the
// body into the omnibox, send PATCHes it), move domain (sheet → PATCH), and
// delete. One hook so both surfaces stay in lockstep on payload shapes.

import { useCallback, useState } from "react";
import type { NotesController } from "./useNotes";

export interface EditingNote {
  id: string;
  body: string;
}

export interface MoveTarget {
  id: string;
  domain: string;
  destination: string | null;
}

export interface NoteActions {
  editing: EditingNote | null;
  startEdit(id: string, body: string): void;
  cancelEdit(): void;
  /** Sends PATCH {body} for the note under edit, then leaves edit mode. */
  submitEdit(body: string): Promise<void>;
  moveTarget: MoveTarget | null;
  startMove(target: MoveTarget): void;
  cancelMove(): void;
  /** Sends PATCH {domain, destination}; explicit null clears the destination. */
  submitMove(domain: string, destination: string | null): Promise<void>;
  remove(id: string): Promise<void>;
}

export function useNoteActions(notes: NotesController): NoteActions {
  const [editing, setEditing] = useState<EditingNote | null>(null);
  const [moveTarget, setMoveTarget] = useState<MoveTarget | null>(null);

  const startEdit = useCallback((id: string, body: string) => setEditing({ id, body }), []);
  const cancelEdit = useCallback(() => setEditing(null), []);

  const submitEdit = useCallback(
    async (body: string) => {
      if (editing === null) return;
      await notes.update(editing.id, { body });
      setEditing(null);
    },
    [editing, notes],
  );

  const startMove = useCallback((target: MoveTarget) => setMoveTarget(target), []);
  const cancelMove = useCallback(() => setMoveTarget(null), []);

  const submitMove = useCallback(
    async (domain: string, destination: string | null) => {
      if (moveTarget === null) return;
      await notes.update(moveTarget.id, { domain, destination });
      setMoveTarget(null);
    },
    [moveTarget, notes],
  );

  const remove = useCallback(async (id: string) => notes.remove(id), [notes]);

  return {
    editing,
    startEdit,
    cancelEdit,
    submitEdit,
    moveTarget,
    startMove,
    cancelMove,
    submitMove,
    remove,
  };
}
