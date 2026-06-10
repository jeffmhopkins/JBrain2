// Full-screen note editor: the topmost tree layer. Editing never drops you
// back to home — it slides over wherever you are (stream, note view, search)
// and returns you there on save/cancel (settled in Phase 2 polish).

import { useEffect, useRef, useState } from "react";
import type { EditingNote } from "../notes/useNoteActions";
import { ChevronLeftIcon } from "./icons";

interface EditLayerProps {
  editing: EditingNote;
  onCancel: () => void;
  onSave: (body: string) => void;
}

export function EditLayer({ editing, onCancel, onSave }: EditLayerProps) {
  const [body, setBody] = useState(editing.body);
  const areaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    areaRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const trimmed = body.trim();
  const savable = trimmed !== "" && trimmed !== editing.body.trim();

  return (
    <section className="edit-layer" aria-label="Edit note">
      <header className="top-bar">
        <button type="button" className="back-btn" onClick={onCancel} aria-label="Cancel edit">
          <ChevronLeftIcon size={22} />
          <span className="screen-title">Edit note</span>
        </button>
        <div className="top-bar-right">
          <button
            type="button"
            className="edit-save"
            disabled={!savable}
            onClick={() => onSave(trimmed)}
          >
            Save
          </button>
        </div>
      </header>
      <textarea
        ref={areaRef}
        className="edit-area"
        aria-label="Note body"
        value={body}
        onChange={(e) => setBody(e.target.value)}
      />
    </section>
  );
}
