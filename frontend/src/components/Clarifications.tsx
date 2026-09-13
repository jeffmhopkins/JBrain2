// "Answers you gave" — the D6 clarification blocks of one note, each erasable.
//
// D6 is a STORAGE decision and says the note screen does not change, so this is not a
// rendering of the blocks: the body above already shows them as prose, composed in, and
// that stays exactly as it was. This is the ERASER, and it needs somewhere to live.
//
// W2 recorded the obligation in so many words — "the wave that ships the writer ships
// the eraser", because on a box with no terminal (CLAUDE.md #10) an unredactable field
// is not a limit the owner can work around. W3 shipped the API half only: `GET`/`DELETE
// /notes/{id}/clarifications` exist and are tested, but nothing in the PWA could issue
// the DELETE and the debug API exposes no generic HTTP verb (its scopes are `llm.*`,
// `sql.read`, `logs.read`, `host.*`, `web.fetch`). A password typed into an answer was
// removable only by deleting the whole note, losing the body and the graph with it.
//
// Why a listing and not a control on the prose: the ids exist nowhere else. The body is
// one composed string, so a block in it cannot be named, and an id you cannot name is a
// block you cannot redact. That is the same reason the listing route was built.
//
// Collapsed by default and absent entirely when the note has none, so the ordinary note
// — which is every note that was never asked about — reads exactly as before.

import { useCallback, useEffect, useState } from "react";
import { type ClarificationOut, api } from "../api/client";

/** The date line on a block, in the register the rest of the note screen uses. */
function whenLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function Block({
  block,
  onErase,
}: {
  block: ClarificationOut;
  onErase: () => Promise<void>;
}) {
  // The tap-again confirm the rest of the app uses for a destructive act (DESIGN.md).
  // Erasing re-drives ingestion, so it is not undoable from here — and unlike a note
  // delete it is not even soft.
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  return (
    <li className="clar-row">
      <div className="clar-qa">
        <span className="clar-q">{block.question}</span>
        <span className="clar-a">{block.answer}</span>
      </div>
      <div className="clar-foot">
        <span className="clar-when">{whenLabel(block.created_at)}</span>
        <button
          type="button"
          className={`clar-erase${armed ? " clar-armed" : ""}`}
          disabled={busy}
          onClick={async () => {
            if (!armed) {
              setArmed(true);
              return;
            }
            setBusy(true);
            try {
              await onErase();
            } finally {
              setBusy(false);
              setArmed(false);
            }
          }}
        >
          {armed ? "tap again — erases this answer from the note" : "erase"}
        </button>
      </div>
    </li>
  );
}

export function Clarifications({
  noteId,
  onErased,
}: {
  noteId: string | null;
  /** The note as it now reads, so the body above updates without a second fetch. */
  onErased: (body: string) => void;
}) {
  const [blocks, setBlocks] = useState<ClarificationOut[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    if (noteId === null) return;
    try {
      setBlocks(await api.listClarifications(noteId));
      setFailed(false);
    } catch {
      // A note with no blocks is the overwhelmingly common case, so a failure here must
      // not put an error where almost every note shows nothing at all.
      setFailed(true);
    }
  }, [noteId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (noteId === null || failed || blocks === null || blocks.length === 0) return null;

  return (
    <section className="clar-panel">
      <button
        type="button"
        className="clar-toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        Answers you gave
        <span className="clar-count">{blocks.length}</span>
      </button>
      {open && (
        <>
          <p className="clar-hint">
            These are part of the note — searchable and quotable, like the rest of it. Erase one to
            take it out for good.
          </p>
          <ul className="clar-list">
            {blocks.map((b) => (
              <Block
                key={b.id}
                block={b}
                onErase={async () => {
                  const note = await api.deleteClarification(noteId, b.id);
                  setBlocks((bs) => (bs ?? []).filter((x) => x.id !== b.id));
                  onErased(note.body);
                }}
              />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
