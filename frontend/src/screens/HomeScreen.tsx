import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { FullBrainSurface } from "../agent/FullBrainSurface";
import { type FullBrainDeps, useFullBrain } from "../agent/useFullBrain";
import { Omnibox } from "../components/Omnibox";
import { Stream } from "../components/Stream";
import { TopBar } from "../components/TopBar";
import { MODES, type SegState } from "../notes/modes";
import type { NoteActions } from "../notes/useNoteActions";
import type { NotesController, StreamItem } from "../notes/useNotes";

const TOAST_MS = 4000;
// The calendar's agent handoff is owner-only and may read every domain, so a
// session it auto-starts spans all of them (no scope picker for the owner).
const ALL_DOMAINS = ["general", "health", "finance", "location"];

interface HomeScreenProps {
  notes: NotesController;
  actions: NoteActions;
  onOpenNote: (item: StreamItem) => void;
  /** Open a Full Brain source note by id (from a Worked-block card). */
  onOpenNoteById?: (noteId: string) => void;
  /** Open an entity page by id (from a Full Brain response chip). */
  onOpenEntity?: (entityId: string) => void;
  onOpenSearch: () => void;
  onOpenLauncher: () => void;
  /** A handoff (e.g. the calendar's reschedule) that flips to Full Brain and
   * seeds the composer with this prompt; cleared via onComposeConsumed. */
  composePrompt?: string | null;
  onComposeConsumed?: () => void;
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
  onOpenNoteById,
  onOpenEntity,
  composePrompt,
  onComposeConsumed,
  fbDeps,
}: HomeScreenProps) {
  const [seg, setSeg] = useState<SegState>({ row: "main", mode: "entry" });
  const [pendingDraft, setPendingDraft] = useState("");
  const composingRef = useRef(false);
  const [toast, setToast] = useState<Toast | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // A compose handoff (the calendar's reschedule/cancel/ask) flips to Full Brain
  // and hands the prompt to the omnibox; the owner reviews and sends it.
  useEffect(() => {
    if (composePrompt) {
      setSeg({ row: "main", mode: "fullbrain" });
      setPendingDraft(composePrompt);
      composingRef.current = true;
      onComposeConsumed?.();
    }
  }, [composePrompt, onComposeConsumed]);
  const clearDraft = useCallback(() => setPendingDraft(""), []);
  // Full Brain is integral to the home page: the transcript and its lateral
  // panels render in the body while the omnibox below acts as its composer. The
  // controller only does work while the mode is on screen.
  const fb = useFullBrain(seg.mode === "fullbrain", fbDeps);

  // The handoff needs a session to send into. If Full Brain loaded with none
  // (it would otherwise drop the owner on the scope picker), start an all-domains
  // session so the seeded request is ready to send.
  useEffect(() => {
    if (!composingRef.current || seg.mode !== "fullbrain") return;
    if (fb.active) {
      composingRef.current = false;
    } else if (fb.panel === "sessions") {
      composingRef.current = false;
      void fb
        .create({ domain_scopes: ALL_DOMAINS })
        .then(fb.open)
        .catch(() => {});
    }
  }, [seg.mode, fb.active, fb.panel, fb.create, fb.open]);

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

  // In Full Brain the session's name owns the top bar (a tap reopens the list),
  // so the transcript starts right under it; other modes keep the wordmark.
  const fbSession =
    seg.mode === "fullbrain"
      ? {
          title: fb.active ? fb.active.title || "Untitled session" : "Full Brain",
          onOpen: () => fb.setPanel("sessions"),
        }
      : undefined;

  return (
    <>
      <TopBar syncStatus={notes.syncStatus} onBolt={onOpenLauncher} session={fbSession} />
      {seg.mode === "fullbrain" ? (
        <FullBrainSurface fb={fb} onOpenNote={onOpenNoteById} onOpenEntity={onOpenEntity} />
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
        draft={pendingDraft}
        onConsumeDraft={clearDraft}
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
