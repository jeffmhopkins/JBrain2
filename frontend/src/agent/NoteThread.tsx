// The note's own conversation, rendered ON the note screen (mock
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

import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { type NoteThreadOut, api } from "../api/client";
import { SendIcon, StopIcon } from "../components/icons";
import { AgentTranscript } from "./FullBrainSurface";
import { answeredCount } from "./asked";
import { type FullBrainDeps, useFullBrain } from "./useFullBrain";

export interface NoteThreadProps {
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
}

export function NoteThread({
  noteId,
  hidden,
  onOpenNote,
  onOpenEntity,
  fbDeps,
  lookupThread,
}: NoteThreadProps): ReactNode {
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
  useEffect(() => {
    if (sessionId !== null) requestOpen(sessionId);
  }, [sessionId, requestOpen]);

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

  const open = fb.openQuestions;
  const answered = answeredCount(open, fb.answers);
  // An answers-only reply is a real turn (§3b I7), so SEND is live on an empty box the
  // moment one candidate is picked.
  const carrying = answered > 0;
  const canSend = !fb.busy && (text.trim() !== "" || carrying);

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
          looked && thread === null
            ? "No conversation yet — the box reads a note once it has indexed it, and the thread opens here."
            : "Opening this note's conversation…"
        }
        emptyText="Say something about this note — it reads the note with you."
      />

      {/* The composer, inline. A follow-up goes into THIS thread; there is no handoff to
          home's conversation surface, which is the whole of what the owner reversed. */}
      <div className="dock note-thread-dock">
        <div className="omnibox">
          <div className="omnibox-body note-thread-composer">
            {/* The carry strip: what rides the next send (§3b I7). Same words and same
                register as the omnibox's, because it is the same contract — the block
                above is inert and this send is its one submit. */}
            {open.length > 0 && (
              <output className="omni-carry">
                <span className="omni-carry-n">
                  {answered} of {open.length}
                </span>
                {answered === 0
                  ? "answered — answer above, or just reply"
                  : "answered — rides with your next send"}
              </output>
            )}
            <textarea
              ref={inputRef}
              className="composer-input"
              placeholder="Reply in this note's thread…"
              value={text}
              onChange={(e) => setText(e.target.value)}
              aria-label="Composer"
            />
            <div className="composer-foot">
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
  );
}
