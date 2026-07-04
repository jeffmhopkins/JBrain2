import { useCallback, useEffect, useRef, useState } from "react";
import { FullBrainSurface } from "../agent/FullBrainSurface";
import type { AppointmentRef } from "../agent/types";
import { type FullBrainDeps, useFullBrain } from "../agent/useFullBrain";
import { useReadAloud } from "../agent/useReadAloud";
import { Omnibox } from "../components/Omnibox";
import { Stream } from "../components/Stream";
import { TopBar } from "../components/TopBar";
import type { SegState } from "../notes/modes";
import type { NoteActions } from "../notes/useNoteActions";
import type { NotesController, StreamItem } from "../notes/useNotes";

const TOAST_MS = 4000;

/** A calendar → Full Brain handoff: the prose that seeds the composer plus the
 * appointment it's about (the agent resolves the id; the owner sees the pill). */
export interface ComposeHandoff {
  text: string;
  appt?: AppointmentRef;
}

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
   * seeds the composer, attaching the appointment pill; cleared via
   * onComposeConsumed. */
  compose?: ComposeHandoff | null;
  onComposeConsumed?: () => void;
  /** A Tasks run → open its session: flips to the matching conversation tab
   * (Research for jerv/teacher/archivist, Full Brain for curator) and opens it.
   * Cleared via onOpenSessionConsumed. */
  openSession?: { id: string; agent: string } | null;
  onOpenSessionConsumed?: () => void;
  /** Injected in tests; defaults to the live API client. */
  fbDeps?: FullBrainDeps;
}

interface Toast {
  message: string;
  /** Single action max (docs/reference/DESIGN.md "Toasts"); here it's the hide undo. */
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
  compose,
  onComposeConsumed,
  openSession,
  onOpenSessionConsumed,
  fbDeps,
}: HomeScreenProps) {
  const [seg, setSeg] = useState<SegState>({ row: "main", mode: "entry" });
  const [pendingDraft, setPendingDraft] = useState("");
  // The appointment a calendar handoff is about — shown as a composer pill and
  // sent with the next turn so the agent resolves it (not by title).
  const [pendingAppt, setPendingAppt] = useState<AppointmentRef | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // A compose handoff (the calendar's reschedule/cancel/ask) flips to Full Brain
  // and hands the prompt to the omnibox; the owner reviews and sends it. The
  // surface auto-opens (or starts) a Curator chat to send into.
  useEffect(() => {
    if (compose) {
      setSeg({ row: "main", mode: "fullbrain" });
      setPendingDraft(compose.text);
      setPendingAppt(compose.appt ?? null);
      onComposeConsumed?.();
    }
  }, [compose, onComposeConsumed]);
  const clearDraft = useCallback(() => setPendingDraft(""), []);
  const clearAppt = useCallback(() => setPendingAppt(null), []);
  // Research and Full Brain are both conversation surfaces, integral to the home
  // page: the transcript and its lateral panels render in the body while the
  // omnibox below acts as the composer. The controller only works while a
  // conversation tab is on screen, and auto-opens that tab's last chat (or starts
  // a fresh one) on entry.
  const convMode = seg.mode === "research" || seg.mode === "fullbrain" ? seg.mode : null;
  const fb = useFullBrain(convMode, fbDeps, true);

  // Local read-aloud: when the owner has enabled read-aloud, the omnibox offers a volume
  // toggle that speaks each completed turn on this device (browser TTS).
  const readAloud = useReadAloud();
  // Speak a completed assistant turn when playback is on. Fires on the busy → idle edge (a
  // turn just settled), only for the chat currently on screen (never a background turn),
  // and only a cleanly-finished, non-empty answer (never a stopped / errored one).
  const prevBusyRef = useRef(false);
  const streamingSessionRef = useRef<string | null>(null);
  if (fb.activeTurn) streamingSessionRef.current = fb.activeTurn.sessionId;
  useEffect(() => {
    const wasBusy = prevBusyRef.current;
    prevBusyRef.current = fb.busy;
    if (!wasBusy || fb.busy) return; // only the settle edge
    const sid = streamingSessionRef.current;
    streamingSessionRef.current = null;
    if (!readAloud.available || !readAloud.on) return;
    if (!sid || sid !== fb.active?.id) return; // a turn in a chat you've navigated away from
    const last = fb.messages[fb.messages.length - 1];
    if (
      last?.role === "assistant" &&
      !last.streaming &&
      last.text.trim() &&
      last.stopReason !== "stopped" &&
      last.stopReason !== "error"
    ) {
      readAloud.speak(last.text);
    }
  }, [fb.busy, fb.messages, fb.active, readAloud.available, readAloud.on, readAloud.speak]);

  // A Tasks run → open its session: flip to the conversation tab that hosts the
  // session's persona, then open it by id (the controller suppresses the tab's
  // auto-open of the latest chat until the requested one loads).
  // biome-ignore lint/correctness/useExhaustiveDependencies: fb methods are recreated each render; keying on them would re-fire the handoff.
  useEffect(() => {
    if (!openSession) return;
    setSeg({ row: "main", mode: openSession.agent === "curator" ? "fullbrain" : "research" });
    fb.requestOpen(openSession.id);
    fb.setPanel("none");
    onOpenSessionConsumed?.();
  }, [openSession, onOpenSessionConsumed]);

  // Re-clicking the conversation tab you're already on starts a fresh chat (a new
  // Jerv in Research, a full-domain Curator in Full Brain); reuse handles empties.
  // Kept in a ref so the segment handler stays stable without going stale.
  const segRef = useRef(seg);
  segRef.current = seg;
  // The appointment pill belongs to a Full Brain turn; drop it if the owner
  // navigates to another mode so it can't leak into an unrelated send. (A mode
  // switch the handoff itself drives uses setSeg directly, keeping the pill.)
  const changeSeg = useCallback(
    (next: SegState) => {
      if (next.mode !== "fullbrain") setPendingAppt(null);
      const reclick =
        (next.mode === "research" || next.mode === "fullbrain") &&
        next.mode === segRef.current.mode;
      if (reclick) fb.startFresh();
      setSeg(next);
    },
    [fb.startFresh],
  );

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

  // Research and Full Brain are conversation surfaces; everything else is capture.
  const conversational = seg.mode === "research" || seg.mode === "fullbrain";

  // The Research tab reads "Teacher" while a Teacher chat is open — still the
  // research slot, just renamed; every other tab keeps the mode's own label.
  const segLabels =
    seg.mode === "research" && fb.active?.agent === "teacher" ? { research: "Teacher" } : undefined;

  // In a conversation tab the open chat's name owns the top bar (a tap reopens the
  // list), so the transcript starts right under it; other modes keep the wordmark.
  // The empty fallback names the tab, and a Teacher chat reads "Teacher".
  const convTitleFallback =
    seg.mode === "research" ? (fb.active?.agent === "teacher" ? "Teacher" : "Research") : "Brain";
  const fbSession = conversational
    ? {
        title: fb.active ? fb.active.title || convTitleFallback : convTitleFallback,
        onOpen: () => fb.setPanel("sessions"),
      }
    : undefined;

  return (
    <>
      <TopBar syncStatus={notes.syncStatus} onBolt={onOpenLauncher} session={fbSession} />
      {conversational ? (
        <FullBrainSurface
          fb={fb}
          onOpenNote={onOpenNoteById}
          onOpenEntity={onOpenEntity}
          // Enacting a correction creates a note out of band; refresh the stream
          // now so it's already there when the owner flips back to entry mode.
          onProposalEnacted={() => void notes.refresh()}
        />
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
        onSegChange={changeSeg}
        onSend={(input) => void notes.send(input)}
        onConversation={(body, files) => {
          // The omnibox is the conversation surface's composer: a send streams
          // into the transcript above (Research → Jerv/Teacher, Full Brain →
          // Curator). The appointment pill only rides a Full Brain handoff; staged
          // files ride as chat attachments. The pill is dropped once a send is
          // under way, but the box keeps the files itself until the send confirms.
          if (!conversational) return Promise.resolve(false);
          const ok = fb.send(body, {
            ...(pendingAppt ? { appointmentId: pendingAppt.id } : {}),
            ...(files.length ? { files } : {}),
          });
          setPendingAppt(null);
          return ok;
        }}
        busy={conversational && fb.busy}
        // Conversation surfaces only: the Stop button aborts the live turn, and the
        // context meter shows how full the model's window is getting.
        onStop={conversational ? fb.stop : undefined}
        contextUsage={conversational ? fb.usage : null}
        // Conversation surfaces only, and only when read-aloud is enabled: a volume toggle
        // left of the context meter that speaks completed turns locally; off stops it now.
        readAloud={
          conversational && readAloud.available
            ? { on: readAloud.on, onToggle: readAloud.toggle }
            : undefined
        }
        onOpenLauncher={onOpenLauncher}
        labels={segLabels}
        // Conversation tabs only: a horizontal swipe across the omnibox shuttles
        // the lateral panels (right→Sessions, left→Proposals; the opposite swipe
        // sends the open one back). The transcript itself no longer swipes.
        onLateralSwipe={
          conversational
            ? (dx) => {
                if (fb.panel === "none") fb.setPanel(dx > 0 ? "sessions" : "proposals");
                else if (fb.panel === "sessions" && dx < 0) fb.setPanel("none");
                else if (fb.panel === "proposals" && dx > 0) fb.setPanel("none");
              }
            : undefined
        }
        draft={pendingDraft}
        onConsumeDraft={clearDraft}
        apptRef={pendingAppt}
        onClearApptRef={clearAppt}
        // Capture modes always keep their attach (note attachments). A conversation
        // mode offers it when the agent's model is vision-capable — OR, in jerv's
        // research mode, when the on-box image tools are configured: jerv can then
        // analyze_image / edit_image an attachment by id even without seeing it. The
        // curator (fullbrain) has no image tools, so there it still needs vision;
        // otherwise the paperclip is simply hidden.
        attachEnabled={
          !conversational || fb.supportsVision || (seg.mode === "research" && fb.canEditImages)
        }
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
