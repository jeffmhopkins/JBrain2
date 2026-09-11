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
import type { AskCandidate, AskedQuestion, SentOutcome } from "./asked";

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

/** What a FROZEN row says about itself, and the whole of R3f's second review, finding 1.
 *
 * The row has to distinguish three things the old rendering collapsed into one: the words
 * that were paired to it, a prose-only reply whose words are the turn itself, and a
 * question the send LEFT OPEN. Saying "answered in your reply" over the third is a lie
 * the owner cannot check and the agent immediately contradicts — it re-asks exactly those
 * questions on its next turn, because `owner_reply_notice` was told the truth.
 *
 * A pair claimed with blank words (a reply turn hand-edited to `A:` and nothing) falls to
 * the middle line rather than an empty row: the pair IS on the turn, it just says
 * nothing. */
function Settled({ outcome }: { outcome: SentOutcome }): ReactNode {
  if (outcome.kind === "open") {
    return <p className="fb-q-open">still open — not answered in your reply</p>;
  }
  if (outcome.kind === "paired" && outcome.answer !== "") {
    return <p className="fb-q-answered">{outcome.answer}</p>;
  }
  return <p className="fb-q-answered">answered in your reply</p>;
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
  outcome,
  readOnly,
}: {
  question: AskedQuestion;
  answer: string;
  onAnswer: (questionId: string, answer: string) => void;
  /** What the reply that settled this block did to THIS question — null while it is
   * live. Its presence is what freezes the row. */
  outcome: SentOutcome | null;
  /** The row can be READ but not answered here — see `QuestionBlock`. */
  readOnly: boolean;
}): ReactNode {
  const q = question;
  const frozen = outcome !== null;
  const [escaped, setEscaped] = useState(false);
  const picked = q.candidates.some((c) => c.value === answer);
  // Revealed by a tap, and revealed anyway while it holds words that are not a candidate
  // — a draft restored after a failed send has to come back visible, not stranded.
  const typing = q.candidates.length > 0 && (escaped || (answer !== "" && !picked));
  // The question and what it blocks, and nothing to answer WITH. Not disabled controls:
  // a greyed candidate invites a tap that cannot work, and the whole point of the state
  // is that this row has no id worth posting. The block's foot says where to answer.
  if (readOnly) {
    return (
      <div className="fb-q">
        {q.blocks && <p className="fb-q-why">blocks · {q.blocks}</p>}
        <p className="fb-q-text">{q.question}</p>
      </div>
    );
  }
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
          </div>
          {/* A frozen block whose answer was none of the candidates (typed free text, or
              a set answered from another device) still says what landed; a row the reply
              did not answer says THAT. Below the chips rather than among them: it is a
              sentence about the row, not one more thing to wrap in the candidate flow. */}
          {outcome !== null && !(outcome.kind === "paired" && picked) && (
            <Settled outcome={outcome} />
          )}
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
      ) : outcome !== null ? (
        <Settled outcome={outcome} />
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

/** The whole block: the header's count, a row per question, and the foot that says what
 * the block does not do.
 *
 * **READ-ONLY is a third state, and it is the deploy window** (R3f's fourth review,
 * finding 2). Every ask persisted before the id echo shipped has a step carrying the
 * model's raw arguments and no ids, while its ledger row holds the real `q########` ones —
 * so a thread left waiting across the deploy would render tappable rows whose ids are
 * `asked.askedQuestions`' positional stand-ins. Sending those posts ids no open question
 * has: `clarify._pair` drops every one, `claim_waiting` consumes the set anyway, nothing
 * reaches the note, and the turn text then degrades to bare prose — which the frozen block
 * reads back as "answered in your reply" over rows that were never answered. Day one on
 * the live box, on the one screen the owner has.
 *
 * So the questions are SHOWN and nothing is offered to answer them with. The composer
 * still works: free text alone is `_pair`'s degrade and answers the oldest open question,
 * which is the first row here, so he is never stuck and nothing can be dropped as an
 * unknown id. **This state can be deleted once no `waiting_on_owner` thread predates the
 * echo** — with it, `askStep`'s legacy fallback and `asktools._refused`'s empty record. */
export function QuestionBlock({
  questions,
  answers,
  onAnswer,
  sent,
  readOnly = false,
}: {
  questions: readonly AskedQuestion[];
  /** The live draft, keyed by question id — the caller holds it until the composer
   * sends. On a frozen block this is the WORDS of `sent`, so a candidate the owner
   * tapped still draws as picked. */
  answers: Readonly<Record<string, string>>;
  /** Record one answer. LOCAL STATE ONLY — the caller holds it until the composer sends. */
  onAnswer: (questionId: string, answer: string) => void;
  /** What the reply that settled this block did to each question (`asked.sentOutcomes`),
   * or null while the block is live. Its presence is what freezes the block, and its
   * contents are what the header is allowed to claim. */
  sent: Readonly<Record<string, SentOutcome>> | null;
  /** The block can be read but not answered — see above. Live blocks only. */
  readOnly?: boolean;
}): ReactNode {
  if (questions.length === 0) return null;
  const n = questions.length;
  // What was ACTUALLY answered, never the size of the set. A send that answers one of
  // three is one of three on the header too — the count is the first thing the owner
  // reads, and "3 questions · answered" over a partial send is the same false report the
  // rows used to make, made once more in the loudest place on the block. A row `sent`
  // somehow has no entry for counts as OPEN, which is the honest side to fail to.
  const answered =
    sent === null ? 0 : questions.filter((q) => (sent[q.id]?.kind ?? "open") !== "open").length;
  return (
    <section
      className={`fb-qblock${sent !== null ? " fb-qblock-done" : ""}`}
      aria-label="Questions"
    >
      <p className="fb-qblock-head">
        {n} question{n === 1 ? "" : "s"}
        {sent !== null
          ? answered === n
            ? " · answered"
            : answered === 0
              ? " · still open"
              : ` · ${answered} answered, ${n - answered} still open`
          : readOnly
            ? " · answer in your reply"
            : " · answers ride with your next send"}
      </p>
      {questions.map((q) => (
        <QuestionRow
          key={q.id}
          question={q}
          answer={answers[q.id] ?? ""}
          onAnswer={onAnswer}
          outcome={sent === null ? null : (sent[q.id] ?? { kind: "open" })}
          readOnly={readOnly && sent === null}
        />
      ))}
      {sent === null &&
        (readOnly ? (
          <p className="fb-q-foot">
            This ask predates the update, so it can only be answered in words: reply in the composer
            and your words answer the first question above. The rest stay open, and the agent is
            told so.
          </p>
        ) : (
          <p className="fb-q-foot">Nothing here sends — your answers ride with the composer.</p>
        ))}
    </section>
  );
}
