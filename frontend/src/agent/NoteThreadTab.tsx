// The note screen's Thread tab — the note's own conversation, rendered ON the note
// screen (mock
// `docs/mocks/agent-ingest/a-note-thread.html`, variant A — the owner reversed the
// variant-C gate on 2026-09-14: *"When I click on the note, it should basically open up
// as a normal agent conversation same as jerv… When I go to do a follow-up, it shouldn't
// open in the brain chat. It should open up right there in the note entry chat."*).
//
// It is the SHIPPED transcript, not a second rendering of it: `AgentTranscript` is the
// same component the home conversation surface mounts, so the violet Thought chip, the
// steel Worked chip, the live phase line, the step rows and the question block are one
// definition with two hosts. The only thing written here is the composer, because the
// home composer is the omnibox — the app's primary navigation — and a mode row on a note
// screen would offer to navigate away from the note you are reading.
//
// Named `NoteThreadTab`, not `NoteThread`: `notes/useNoteThreads.ts` already exports a
// `NoteThread` — the waiting-thread record the stream's ask chip is drawn from — and two
// symbols of that name, one a component and one a row of state, is a collision waiting
// for the file that needs both.
//
// What the omnibox has and this deliberately does not: the mode row (above), the
// paperclip (a note's own files are added on the Files tab, which is one tap away and is
// the canonical manager), the context meter and the model pill. None of them is a way
// into or out of the conversation; each is a home-surface control, and a note screen that
// grew them would be the omnibox with a different name.

import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { type NoteThreadOut, api } from "../api/client";
import { useBackLayer } from "../backLayers";
import { SendIcon, StopIcon } from "../components/icons";
import { AgentTranscript, ProposalsAside } from "./FullBrainSurface";
import { answeredCount } from "./asked";
import { type FullBrainDeps, useFullBrain } from "./useFullBrain";

export interface NoteThreadTabProps {
  /** Server note id; null for an outbox row that hasn't synced, which can have no
   * conversation yet. */
  noteId: string | null;
  /** Hidden rather than unmounted while another tab is on screen: a first pass on the
   * box's own GPU runs for minutes, and unmounting would drop the live stream every time
   * the owner looked at the body. */
  hidden: boolean;
  /** A Worked-block source card opens the cited note. */
  onOpenNote?: ((noteId: string) => void) | undefined;
  /** A response entity chip opens the entity page above this layer. */
  onOpenEntity?: ((entityId: string) => void) | undefined;
  /** Injected in tests; defaults to the live API client (as `HomeScreen.fbDeps` does). */
  fbDeps?: FullBrainDeps | undefined;
  /** Injected in tests; defaults to `GET /notes/{id}/thread`. */
  lookupThread?: ((noteId: string) => Promise<NoteThreadOut | null>) | undefined;
  /** How many questions this thread is waiting on, whenever it changes — the count the
   * tab row wears (the mock draws it on `Thread`). Reported upward rather than read
   * twice: the questions are derived from the transcript this component already holds,
   * and a second fetch for a number would be a second source of truth for it. */
  onAskCount?: ((n: number) => void) | undefined;
}

export function NoteThreadTab({
  noteId,
  hidden,
  onOpenNote,
  onOpenEntity,
  fbDeps,
  lookupThread,
  onAskCount,
}: NoteThreadTabProps): ReactNode {
  const [thread, setThread] = useState<NoteThreadOut | null>(null);
  // Distinguishes "no conversation" from "not looked yet", so the empty state never
  // flashes "the box hasn't read this note" at a note whose thread is one tick away.
  const [looked, setLooked] = useState(false);
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // `note_ingest` sits on the Full Brain tab (`useFullBrain.MODE_AGENTS`) — a conversation
  // about the owner's own notes — so that is the group this reads. autoStart stays off:
  // this surface opens ONE session by id and must never fall back to the latest chat.
  const fb = useFullBrain("fullbrain", fbDeps);
  const { requestOpen } = fb;

  const lookup = lookupThread ?? api.noteThread;
  useEffect(() => {
    if (noteId === null) {
      setLooked(true);
      return;
    }
    let stale = false;
    void lookup(noteId)
      .then((found) => {
        if (stale) return;
        setThread(found);
        setLooked(true);
      })
      .catch(() => {
        // A note with no thread is the ordinary case this returns null for, and a failed
        // lookup is indistinguishable from it as far as this tab can tell.
        if (!stale) setLooked(true);
      });
    return () => {
      stale = true;
    };
  }, [noteId, lookup]);

  const sessionId = thread?.session_id ?? null;
  // The id is the trigger, and it is the ONLY dependency: `requestOpen` is a plain
  // function recreated on every render, so keying on it would re-fire the handoff each
  // render — and while the session list is still loading each of those fires a fresh
  // `listSessions()`. HomeScreen keys its Tasks handoff the same way for the same reason.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the id is the trigger, not a read.
  useEffect(() => {
    if (sessionId !== null) requestOpen(sessionId);
  }, [sessionId]);

  // The typed half of a send that reached the server not at all comes back here — the
  // same seam the omnibox takes it on. Put it ABOVE anything typed since, never over it:
  // the composer is live throughout a turn, so the owner may well have started the next
  // message while the failed one was in flight.
  const restored = fb.restoredText;
  const consumeRestored = fb.consumeRestoredText;
  useEffect(() => {
    if (restored === "") return;
    setText((cur) => (cur.trim() === "" ? restored : `${restored}\n\n${cur}`));
    consumeRestored();
  }, [restored, consumeRestored]);

  // Grow the box with the text, as the omnibox does. `text` is a deliberate trigger: the
  // content height is read off the DOM, which only reflects it after a render.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-measure on text change
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [text]);

  // A note thread stages proposals: `prefs_write` is on the `note_ingest` on-reply
  // allowlist and its kind (`owner-prefs`) is NOT in `INLINE_KINDS`, so its turn draws
  // the NAVIGATIONAL "Review proposal" chip — which opens this panel and nothing else.
  // Without it that chip would be a dead tap, which is the kind of affordance this wave
  // exists to remove. (`merge_entities` stages one too, but `merge` renders inline.)
  // The panel pins to this component's own `.fb-shell`. Registered as a back layer while
  // it is open so the platform Back gesture closes the PANEL, not the note under it.
  const proposalOpen = fb.panel === "proposals";

  const questions = fb.openQuestions;
  const answered = answeredCount(questions, fb.answers);
  const asking = questions.length;
  useEffect(() => onAskCount?.(asking), [asking, onAskCount]);
  // An answers-only reply is a real turn (§3b I7), so SEND is live on an empty box the
  // moment one candidate is picked. `fb.canSend` is the rest of it: a session is open and
  // no turn is in flight.
  const carrying = answered > 0;
  const canSend = fb.canSend && (text.trim() !== "" || carrying);
  // No conversation, no composer. A box that cannot send anywhere is worse than none —
  // the same rule the deleted "Add a thought" button followed, and the empty state above
  // already says why there is nothing here.
  const noThread = looked && thread === null;

  function send(): void {
    if (!canSend) return;
    const body = text.trim();
    // Clear at once: the transcript shows the message optimistically, and leaving it in
    // the box makes it read twice while a local model spins up. A send that reached
    // nothing hands the words back through `restoredText` above.
    setText("");
    void fb.send(body);
  }

  return (
    <div className="fb-shell note-thread" hidden={hidden}>
      <AgentTranscript
        fb={fb}
        onOpenNote={onOpenNote}
        onOpenEntity={onOpenEntity}
        noSessionText={
          noThread
            ? "No conversation yet — the box reads a note once it has indexed it, and the thread opens here."
            : "Opening this note's conversation…"
        }
        emptyText="Say something about this note — it reads the note with you."
      />

      <ProposalsAside fb={fb} />
      {proposalOpen && (
        <BackLayer
          onClose={() => {
            // Climb the panel's own layers first — an open proposal sits atop the list —
            // the same order HomeScreen gives the gesture on the home surface.
            if (fb.openProposal !== null) fb.setOpenProposal(null);
            else fb.setPanel("none");
          }}
        />
      )}

      {/* The composer, inline. A follow-up goes into THIS thread; there is no handoff to
          home's conversation surface, which is the whole of what the owner reversed. */}
      {!noThread && (
        <div className="dock note-thread-dock">
          <div className="omnibox">
            <div className="omnibox-body note-thread-composer">
              {/* The carry strip: what rides the next send (§3b I7). Same words and same
                register as the omnibox's, because it is the same contract — the block
                above is inert and this send is its one submit. */}
              {questions.length > 0 && (
                <output className="omni-carry">
                  <span className="omni-carry-n">
                    {answered} of {questions.length}
                  </span>
                  {answered === 0
                    ? "answered — answer above, or just reply"
                    : "answered — rides with your next send"}
                </output>
              )}
              <textarea
                ref={inputRef}
                className="composer-input"
                placeholder="Reply about this note…"
                value={text}
                onChange={(e) => setText(e.target.value)}
                aria-label="Composer"
              />
              <div className="composer-foot">
                {/* The omnibox foot's microcopy line, in this surface's words. It is what
                  says WHERE a reply goes — the one thing the shipped hand-off got wrong
                  — and it is the mock's own line. */}
                <span className="note-thread-note">
                  <span className="note-thread-dot" aria-hidden="true" />
                  replying into this note's thread
                </span>
                <div className="foot-icons">
                  {fb.busy ? (
                    <button
                      type="button"
                      className="icon-btn stop-btn"
                      aria-label="Stop generating"
                      onClick={fb.stop}
                    >
                      <StopIcon size={24} />
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="icon-btn send-btn"
                      aria-label="Send"
                      onClick={send}
                      disabled={!canSend}
                    >
                      <SendIcon size={24} />
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** `useBackLayer` registers for as long as it is MOUNTED, and App pops the top of that
 * stack before it descends into the screen stack — so a layer that registered while
 * closed would swallow the gesture and close nothing. Hence a component: it mounts only
 * while the panel is open, which is exactly the window the registration should cover.
 * The same shape the shared `<Sheet>` uses. */
function BackLayer({ onClose }: { onClose: () => void }): null {
  useBackLayer(onClose);
  return null;
}
