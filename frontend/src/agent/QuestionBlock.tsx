// The question block: what a note thread shows when its pass ends on an `ask_owner`
// (AGENT_INGEST_REWRITE §3b I6, mock `docs/mocks/agent-ingest-thread/note-thread.html`).
//
// One row per question, each carrying WHY IT BLOCKS, the question in plain words, and its
// answer affordance — tappable candidates where the resolver had them, a text field where
// it did not, and on a candidate row BOTH: "Something else" reveals the same field, so a
// candidate the model's prose lost is still answerable in words (see `QuestionRow`).
// Before this the question reached the owner only as a collapsed Worked step ("Asked you
// a question", the question as its inline arg), so answering meant expanding a disclosure
// to find out what was being asked.
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

import { type ReactNode, useState } from "react";
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

/** One question's row: what it blocks, the question, and its answer affordance.
 *
 * Its own component because the typed ESCAPE is per-question local state, and because
 * that state is UI and nothing else — the answer itself still lives in the caller's
 * draft, so the block stays as inert as I6 requires.
 *
 * **Why a candidate row ALSO gets a way to type** (R3f's review, finding 4). The
 * candidates are not a structured set: `ask_owner.tool` declares `candidates` as a
 * string, the model retypes the resolver's set as prose, and `parseCandidates` splits
 * what it wrote. Measured against real output, one unclosed paren makes a candidate
 * unreachable — and a row that renders candidates OR a field, never both, then has no
 * way at all to say "Dr. Ray Chen". Hardening the parser cannot fix that: every repair
 * available invents candidates the model never wrote, and a candidate the owner taps is
 * a sentence in his own note. The escape is the robust fix — the same field the
 * no-candidate row gets, one tap away — and it also answers the ordinary case the mock
 * never drew: none of the candidates is right. */
function QuestionRow({
  question,
  answer,
  onAnswer,
  frozen,
}: {
  question: AskedQuestion;
  answer: string;
  onAnswer: (questionId: string, answer: string) => void;
  frozen: boolean;
}): ReactNode {
  const q = question;
  const [escaped, setEscaped] = useState(false);
  const picked = q.candidates.some((c) => c.value === answer);
  // Revealed by a tap, and revealed anyway while it holds words that are not a candidate
  // — a draft restored after a failed send has to come back visible, not stranded.
  const typing = q.candidates.length > 0 && (escaped || (answer !== "" && !picked));
  return (
    <div className="fb-q">
      {q.blocks && <p className="fb-q-why">blocks · {q.blocks}</p>}
      <p className="fb-q-text">{q.question}</p>
      {q.candidates.length > 0 ? (
        <>
          <div className="fb-q-opts">
            {q.candidates.map((c, i) => (
              <Candidate
                // The list is parsed from one string and never reorders; two
                // identical candidates would otherwise collide on a value key.
                // biome-ignore lint/suspicious/noArrayIndexKey: fixed, ordered list
                key={i}
                candidate={c}
                picked={answer === c.value}
                frozen={frozen}
                // Tapping the picked one unpicks it: a stray tap has to be undoable
                // for "nothing here commits anything" to be true in practice.
                onPick={() => {
                  setEscaped(false);
                  onAnswer(q.id, answer === c.value ? "" : c.value);
                }}
              />
            ))}
            {!frozen && (
              <button
                type="button"
                className={`fb-q-opt fb-q-else${typing ? " fb-q-picked" : ""}`}
                aria-pressed={typing}
                onClick={() => {
                  setEscaped(!typing);
                  // Opening it drops the pick it replaces; closing it drops the words.
                  // Either way the row is left saying one thing, and both are undoable.
                  if (answer !== "") onAnswer(q.id, "");
                }}
              >
                <span className="fb-q-opt-name">Something else</span>
              </button>
            )}
            {/* A frozen block whose answer was none of the candidates (typed free
                text, or a set answered from another device) still says what landed. */}
            {frozen && answer !== "" && !picked && <p className="fb-q-answered">{answer}</p>}
          </div>
          {typing && !frozen && (
            <input
              className="fb-q-input"
              type="text"
              value={answer}
              autoComplete="off"
              // biome-ignore lint/a11y/noAutofocus: the tap that revealed it asked for it
              autoFocus
              aria-label={q.question}
              onChange={(e) => onAnswer(q.id, e.target.value)}
            />
          )}
        </>
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
      {questions.map((q) => (
        <QuestionRow
          key={q.id}
          question={q}
          answer={answers[q.id] ?? ""}
          onAnswer={onAnswer}
          frozen={frozen}
        />
      ))}
      {!frozen && (
        <p className="fb-q-foot">Nothing here sends — your answers ride with the composer.</p>
      )}
    </section>
  );
}
