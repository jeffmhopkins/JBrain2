import { type CSSProperties, useEffect, useRef, useState } from "react";
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
}

export function HomeScreen({
  notes,
  actions,
  onOpenNote,
  onOpenSearch,
  onOpenLauncher,
}: HomeScreenProps) {
  const [seg, setSeg] = useState<SegState>({ row: "main", mode: "entry" });
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function showToast(message: string) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(message);
    toastTimer.current = setTimeout(() => setToast(null), TOAST_MS);
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
      {conversational ? (
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
            if (item.id !== null) actions.startEdit(item.id, item.body);
          }}
          onMove={(item) => {
            if (item.id !== null)
              actions.startMove({
                id: item.id,
                domain: item.domain,
                destination: item.destination,
              });
          }}
          onDelete={(id) => void actions.remove(id)}
        />
      )}
      <Omnibox
        seg={seg}
        onSegChange={setSeg}
        editing={actions.editing}
        onCancelEdit={actions.cancelEdit}
        onSubmitEdit={(body) => void actions.submitEdit(body)}
        onSend={(input) => void notes.send(input)}
        onConversation={() => showToast("Conversations arrive in Phase 4")}
        onOpenLauncher={onOpenLauncher}
      />
      {toast && <output className="toast">{toast}</output>}
    </>
  );
}
