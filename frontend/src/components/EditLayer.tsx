// Full-screen note editor, "focused writer" design (settled in the Phase 2
// edit-screen review): chrome fades to a whisper of domain + date, the note
// is the screen at a reading size, and done waits in the thumb bar that
// rides above the keyboard. Cancel stays quiet until edits make it ask.

import { useCallback, useEffect, useRef, useState } from "react";
import { DOMAIN_COLOR } from "../notes/modes";
import type { EditingNote } from "../notes/useNoteActions";
import type { StreamAttachment } from "../notes/useNotes";
import { ClipIcon, XIcon } from "./icons";

const DISARM_MS = 3000;

function counts(text: string): { words: number; chars: number } {
  const trimmed = text.trim();
  return {
    words: trimmed === "" ? 0 : trimmed.split(/\s+/).length,
    chars: text.length,
  };
}

interface EditLayerProps {
  editing: EditingNote;
  onCancel: () => void;
  onSave: (body: string) => void;
  /** Uploads immediately to the note (independent of done/cancel). */
  onAddFile: (file: File) => Promise<StreamAttachment>;
  /** Removes immediately from the note. */
  onRemoveAttachment: (attachmentId: string) => Promise<void>;
}

export function EditLayer({
  editing,
  onCancel,
  onSave,
  onAddFile,
  onRemoveAttachment,
}: EditLayerProps) {
  const [body, setBody] = useState(editing.body);
  const [discardArmed, setDiscardArmed] = useState(false);
  const [attachments, setAttachments] = useState<StreamAttachment[]>(editing.attachments);
  const [uploading, setUploading] = useState(0);
  const [removeArmed, setRemoveArmed] = useState<string | null>(null);
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const disarmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function addFiles(list: FileList | null) {
    if (!list) return;
    for (const file of Array.from(list)) {
      setUploading((n) => n + 1);
      try {
        const added = await onAddFile(file);
        setAttachments((prev) => [...prev, added]);
      } catch {
        // Upload failures stay quiet here; the sync dot reports trouble.
      } finally {
        setUploading((n) => n - 1);
      }
    }
  }

  async function removeChip(id: string) {
    if (removeArmed !== id) {
      setRemoveArmed(id);
      setTimeout(() => setRemoveArmed((cur) => (cur === id ? null : cur)), DISARM_MS);
      return;
    }
    setRemoveArmed(null);
    try {
      await onRemoveAttachment(id);
      setAttachments((prev) => prev.filter((a) => a.id !== id));
    } catch {
      // Kept on failure; the chip simply remains.
    }
  }

  const trimmed = body.trim();
  const dirty = trimmed !== editing.body.trim();
  const savable = trimmed !== "" && dirty;
  const { words, chars } = counts(body);

  const disarm = useCallback(() => {
    if (disarmTimer.current !== null) clearTimeout(disarmTimer.current);
    disarmTimer.current = null;
    setDiscardArmed(false);
  }, []);

  useEffect(() => {
    areaRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (disarmTimer.current !== null) clearTimeout(disarmTimer.current);
    };
  }, [onCancel]);

  function close() {
    if (!dirty || discardArmed) {
      onCancel();
      return;
    }
    // Edits make leaving ask first: arm, then auto-disarm.
    setDiscardArmed(true);
    disarmTimer.current = setTimeout(() => setDiscardArmed(false), DISARM_MS);
  }

  const dateLabel = editing.createdAt
    .toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })
    .toLowerCase();

  return (
    <section className="edit-layer" aria-label="Edit note">
      <header className="ed-head">
        <button
          type="button"
          className={`btn-close${discardArmed ? " armed" : ""}`}
          onClick={close}
          aria-label={discardArmed ? "Discard edits" : "Close editor"}
        >
          {discardArmed ? "discard edits?" : <XIcon size={20} />}
        </button>
        <div className="context-line" aria-hidden="true">
          <span className="ed-dot" style={{ background: DOMAIN_COLOR[editing.domain] }} />
          {editing.domain} · {dateLabel}
        </div>
      </header>

      <div className="ed-body">
        <textarea
          ref={areaRef}
          className="ed-page"
          aria-label="Note body"
          value={body}
          placeholder="write…"
          onChange={(e) => {
            setBody(e.target.value);
            disarm();
          }}
        />
      </div>

      {(attachments.length > 0 || uploading > 0) && (
        <div className="ed-attach-row">
          {attachments.map((att) => (
            <button
              key={att.id ?? att.filename}
              type="button"
              className={`ed-chip${removeArmed === att.id ? " armed" : ""}`}
              onClick={() => att.id !== null && void removeChip(att.id)}
            >
              {removeArmed === att.id ? "remove?" : att.filename}
              <XIcon size={13} />
            </button>
          ))}
          {uploading > 0 && <span className="ed-chip ed-chip-busy">uploading…</span>}
        </div>
      )}

      <div className="ed-bar">
        <button
          type="button"
          className="btn-clip"
          aria-label="Attach files"
          onClick={() => fileRef.current?.click()}
        >
          <ClipIcon size={20} />
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            void addFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <span className="ed-counts">
          {words} words · {chars} chars
          {dirty && <span className="ed-unsaved"> · unsaved</span>}
        </span>
        <button
          type="button"
          className={`btn-done${savable ? " ready" : ""}`}
          disabled={!savable}
          onClick={() => onSave(trimmed)}
        >
          done
        </button>
      </div>
    </section>
  );
}
