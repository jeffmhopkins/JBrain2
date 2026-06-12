import { type CSSProperties, useEffect, useRef, useState } from "react";
import { FullBrainSurface } from "../agent/FullBrainSurface";
import { type FullBrainDeps, useFullBrain } from "../agent/useFullBrain";
import { Omnibox } from "../components/Omnibox";
import { Stream } from "../components/Stream";
import { MODES, type SegState } from "../notes/modes";
import type { NoteActions } from "../notes/useNoteActions";
import type { NotesController, StreamItem } from "../notes/useNotes";

const TOAST_MS = 4000;

interface HomeScreenProps {
  notes: NotesController;
  actions: NoteActions;
  onOpenNote: (item: StreamItem) => void;
  onOpenSearch: () => void;
  onOpenLauncher: () => void;
  /** Injected in tests; defaults to the live API client. */
  fbDeps?: FullBrainDeps;
}

interface Toast {
  message: string;
  /** Single action max (docs/DESIGN.md "Toasts"); here it's the hide undo. */
  action?: { label: string; run: () => void };
}

export function HomeScreen({
  notes,
  actions,
  onOpenNote,
  onOpenSearch,
  onOpenLauncher,
  fbDeps,
}: HomeScreenProps) {
  const [seg, setSeg] = useState<SegState>({ row: "main", mode: "entry" });
  const [toast, setToast] = useState<Toast | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Full Brain is integral to the home page: the transcript and its lateral
  // panels render in the body while the omnibox below acts as its composer. The
  // controller only does work while the mode is on screen.
  const fb = useFullBrain(seg.mode === "fullbrain", fbDeps);

  function showToast(message: string, action?: Toast["action"]) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(action ? { message, action } : { message });
    toastTimer.current = setTimeout(() => setToast(null), TOAST_MS);
  }

  function dismissToast() {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(null);
  }

  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    },
    [],
  );

  // Research / Full Brain scope the stream area to that mode's conversation
  // list — empty until Phase 4 ships conversations.
  const conversational = seg.mode === "research" || seg.mode === "fullbrain";
  const meta = MODES[seg.mode];

  return (
    <>
      {seg.mode === "fullbrain" ? (
        <FullBrainSurface fb={fb} />
      ) : conversational ? (
        <main className="stream conv-area">
          <p
            className="conv-empty"
            style={{ "--mode": meta.color, "--mode-tint": meta.tint } as CSSProperties}
          >
            conversations arrive in Phase 4 — typing starts one then
          </p>
        </main>
      ) : (
        <Stream
          items={notes.items}
          onOpenSearch={onOpenSearch}
          onOpenNote={onOpenNote}
          onEdit={(item) => {
            if (item.id !== null)
              actions.startEdit({
                id: item.id,
                body: item.body,
                domain: item.domain,
                createdAt: item.createdAt,
                attachments: item.attachments,
              });
          }}
          onDelete={(id) => void actions.remove(id)}
          onHide={(item) => {
            const id = item.id;
            if (id === null) return;
            void notes.setHidden(id, true);
            showToast("note hidden", {
              label: "undo",
              run: () => {
                dismissToast();
                void notes.setHidden(id, false);
              },
            });
          }}
        />
      )}
      <Omnibox
        seg={seg}
        onSegChange={setSeg}
        onSend={(input) => void notes.send(input)}
        onConversation={(body) => {
          // The omnibox is Full Brain's composer: a send streams into the
          // transcript above. Research's read-only surface is still Phase 4.
          if (seg.mode === "fullbrain") fb.send(body);
          else showToast("Conversations arrive in Phase 4");
        }}
        busy={seg.mode === "fullbrain" && fb.busy}
        onOpenLauncher={onOpenLauncher}
      />
      {toast && (
        <output className="toast">
          {toast.message}
          {toast.action && (
            <button type="button" className="toast-action" onClick={toast.action.run}>
              {toast.action.label}
            </button>
          )}
        </output>
      )}
    </>
  );
}
