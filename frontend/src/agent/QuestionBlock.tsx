// The question block: what a note thread shows when its pass ends on an `ask_owner`
// (AGENT_INGEST_REWRITE §3b I6, mock `docs/mocks/agent-ingest-thread/note-thread.html`).
//
// One row per question, each carrying WHY IT BLOCKS, the question in plain words, and its
// answer affordance — tappable candidates where the resolver had them, a text field where
// it did not. Before this the question reached the owner only as a collapsed Worked step
// ("Asked you a question", the question as its inline arg), so answering meant expanding a
// disclosure to find out what was being asked.
//
// THE BLOCK CANNOT START A TURN, and that is the point of it rather than a detail of it.
// Selecting a candidate or typing in a field is local state: nothing posts, nothing
// enqueues, nothing flips a conversation state, and the component imports no client. A
// half-answered block costs nothing and a stray tap cannot burn a pass (tap a chosen
// candidate again to unpick it).
//
// This is a deliberate departure from the app's other interactive-in-transcript component.
// `InlineProposal` posts its own server-authored outcome back as a follow-up turn, which
// is right for a proposal — there the enact IS the event. Here it would be wrong for an
// arithmetic reason: three answers that each posted would be three turns, three
// clarification blocks and three re-reads of the note, which is exactly the cost the
// batched ask exists to remove (O9). The submit is borrowed from the composer instead
// (§3b I7) — the omnibox send is the one submit in the app, inside a thread as everywhere
// else.
//
// Amber, not the mock's rose, for I1's reason one surface over: rose is the MEDICAL domain
// in the shipped palette, so a rose block says the same thing twice on a medical note and
// something false on a financial one. Amber is the open-ask register the stream chip and
// the inbox already use, and the words carry it either way.

import type { ReactNode } from "react";
import type { AskCandidate, AskedQuestion } from "./asked";

function Candidate({
  candidate,
  picked,
  onPick,
  frozen,
}: {
  candidate: AskCandidate;
  picked: boolean;
  onPick: () => void;
  frozen: boolean;
}): ReactNode {
  return (
    <button
      type="button"
      className={`fb-q-opt${picked ? " fb-q-picked" : ""}`}
      aria-pressed={picked}
      disabled={frozen}
      onClick={onPick}
    >
      <span className="fb-q-opt-name">{candidate.label}</span>
      {candidate.detail && <span className="fb-q-opt-detail">{candidate.detail}</span>}
    </button>
  );
}

export function QuestionBlock({
  questions,
  answers,
  onAnswer,
  frozen,
}: {
  questions: readonly AskedQuestion[];
  /** The draft, keyed by question id. On a frozen block these are the answers that were
   * sent, recovered from the reply turn's own text. */
  answers: Readonly<Record<string, string>>;
  /** Record one answer. LOCAL STATE ONLY — the caller holds it until the composer sends. */
  onAnswer: (questionId: string, answer: string) => void;
  /** The set has been answered (or the thread moved on): show what was said, inert. */
  frozen: boolean;
}): ReactNode {
  if (questions.length === 0) return null;
  const n = questions.length;
  return (
    <section className={`fb-qblock${frozen ? " fb-qblock-done" : ""}`} aria-label="Questions">
      <p className="fb-qblock-head">
        {n} question{n === 1 ? "" : "s"}
        {frozen ? " · answered" : " · answers ride with your next send"}
      </p>
      {questions.map((q) => {
        const answer = answers[q.id] ?? "";
        return (
          <div className="fb-q" key={q.id}>
            {q.blocks && <p className="fb-q-why">blocks · {q.blocks}</p>}
            <p className="fb-q-text">{q.question}</p>
            {q.candidates.length > 0 ? (
              <div className="fb-q-opts">
                {q.candidates.map((c) => (
                  <Candidate
                    key={c.value}
                    candidate={c}
                    picked={answer === c.value}
                    frozen={frozen}
                    // Tapping the picked one unpicks it: a stray tap has to be undoable
                    // for "nothing here commits anything" to be true in practice.
                    onPick={() => onAnswer(q.id, answer === c.value ? "" : c.value)}
                  />
                ))}
                {/* A frozen block whose answer was none of the candidates (typed free
                    text, or a set answered from another device) still says what landed. */}
                {frozen && answer !== "" && !q.candidates.some((c) => c.value === answer) && (
                  <p className="fb-q-answered">{answer}</p>
                )}
              </div>
            ) : frozen ? (
              <p className="fb-q-answered">{answer || "answered in your reply"}</p>
            ) : (
              <input
                className="fb-q-input"
                type="text"
                value={answer}
                autoComplete="off"
                aria-label={q.question}
                onChange={(e) => onAnswer(q.id, e.target.value)}
              />
            )}
          </div>
        );
      })}
      {!frozen && (
        <p className="fb-q-foot">Nothing here sends — your answers ride with the composer.</p>
      )}
    </section>
  );
}
