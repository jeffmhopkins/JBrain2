// The open question set of a note thread, read off the transcript — the data the
// question block renders (AGENT_INGEST_REWRITE §3b I6/I9).
//
// Pure, and reading only what is ALREADY on the wire: the questions are the `ask_owner`
// call's own recorded `args`, which a persisted turn replays exactly as a live one does
// (`useFullBrain.fromTurn`, `transcript.ts`), so a thread reopened weeks later renders
// the same block with no new endpoint and no answer state that lives only in a component.
//
// This module is the frontend mirror of `models/note_conversation.questions_from_args`,
// including its deploy-window fallback for a pre-batch `args["question"]`. Two parsers of
// one shape is a drift risk, and the alternative — a third wire field carrying what the
// args already carry — is worse: it would be a second source of truth for the SET the
// reply path pairs against, and a block that offers a question the ledger no longer holds
// posts an id `_pair` then drops.

import type { ToolActivity, TranscriptMessage } from "./transcript";

/** One tappable candidate of a question: what the resolver already knows about each
 * person the note could mean. `label` is the name, `detail` the parenthetical that tells
 * two same-named people apart, and `value` is what a tap answers WITH. */
export interface AskCandidate {
  label: string;
  detail: string;
  value: string;
}

/** One question of an open set, as `ask_owner` recorded it. */
export interface AskedQuestion {
  id: string;
  question: string;
  /** What the question blocks — the predicate or the resolve call it is stuck on. "" when
   * the model did not say. */
  blocks: string;
  /** The candidates the resolver handed the model, parsed for tapping. Empty means the
   * question takes a typed answer. */
  candidates: AskCandidate[];
}

function oneLine(value: unknown): string {
  return String(value ?? "")
    .split(/\s+/)
    .filter((w) => w !== "")
    .join(" ");
}

/** Split a candidate list on its top-level commas only.
 *
 * The tool teaches the model to write "Sarah Whitfield (sister, 12 notes), Sarah Chen
 * (work, 3 notes)" (`ask_owner.tool`), so the separator and the detail's own punctuation
 * are the same character. Splitting naively turns two candidates into four, and "12
 * notes)" is not something to offer as an answer. */
function splitCandidates(raw: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const ch of raw) {
    if (ch === "(" || ch === "[") depth += 1;
    else if (ch === ")" || ch === "]") depth = Math.max(0, depth - 1);
    else if (ch === "," && depth === 0) {
      parts.push(current);
      current = "";
      continue;
    }
    current += ch;
  }
  parts.push(current);
  return parts.map((p) => p.trim()).filter((p) => p !== "");
}

/** The candidates of one question, as chips. Model-authored free text, so nothing here
 * assumes the shape held: a candidate with no parenthetical is a bare label, and one that
 * is only a parenthetical keeps its own text as the label. */
export function parseCandidates(raw: string): AskCandidate[] {
  const pieces = splitCandidates(oneLine(raw));
  const split = pieces.map((piece) => {
    const m = /^(.*?)\s*\(([^)]*)\)$/.exec(piece);
    const label = m ? (m[1] ?? "").trim() : piece;
    return {
      raw: piece,
      label: label === "" ? piece : label,
      detail: m ? (m[2] ?? "").trim() : "",
    };
  });
  // A tap answers with the NAME, which is what the owner would have typed and what the
  // clarification block reads best as — unless two candidates share one, in which case
  // the name is exactly the thing that does not identify which was tapped, and the whole
  // candidate string is the only unambiguous answer.
  const counts = new Map<string, number>();
  for (const c of split) counts.set(c.label, (counts.get(c.label) ?? 0) + 1);
  return split.map((c) => ({
    label: c.label,
    detail: c.detail,
    value: (counts.get(c.label) ?? 0) > 1 ? c.raw : c.label,
  }));
}

/** The question set an `ask_owner` call's arguments hold, in the order it asked them. */
export function askedQuestions(args: Record<string, unknown> | undefined): AskedQuestion[] {
  const raw = args?.questions;
  if (!Array.isArray(raw)) {
    // A thread can be sitting in `waiting_on_owner` with a pre-batch ledger row; without
    // this the owner sees an empty block where their question is. A positional id is
    // enough — nothing structured can name a row that predates ids.
    const legacy = oneLine(args?.question);
    return legacy ? [{ id: "q1", question: legacy, blocks: "", candidates: [] }] : [];
  }
  const asked: AskedQuestion[] = [];
  raw.forEach((item, i) => {
    if (typeof item !== "object" || item === null) return;
    const row = item as Record<string, unknown>;
    const question = oneLine(row.question);
    if (!question) return;
    asked.push({
      id: oneLine(row.id) || `q${i + 1}`,
      question,
      blocks: oneLine(row.blocks),
      candidates: parseCandidates(oneLine(row.candidates)),
    });
  });
  return asked;
}

/** The LAST succeeded `ask_owner` step of a turn, or null. Last and succeeded for the
 * reason `notes_inbox` filters the same way: a failed call asked nothing, and the reply
 * path pairs against the open set, so a block rendered off any other row would offer
 * questions the ledger does not hold. */
export function askStep(message: TranscriptMessage): ToolActivity | null {
  const asks = message.tools.filter((t) => t.name === "ask_owner" && t.ok === true);
  return asks[asks.length - 1] ?? null;
}

/** The question set a turn ended on, empty when it did not end on one. */
export function turnQuestions(message: TranscriptMessage): AskedQuestion[] {
  const step = askStep(message);
  return step ? askedQuestions(step.args) : [];
}

/** The open question set of a thread: the questions of the LAST message when that message
 * is the ask itself.
 *
 * "Is it last" is the whole test, and it is deliberately not `stop_reason`: a persisted
 * turn replays no stop reason (`fromTurn`), so a reopened waiting thread would look
 * settled. A turn the owner has answered has their reply after it; one they have not is
 * the end of the transcript. That holds live and on reopen, and a thread that asked
 * twice freezes the older block and arms the newer one with no extra state. */
export function openQuestions(messages: readonly TranscriptMessage[]): AskedQuestion[] {
  const last = messages[messages.length - 1];
  if (!last || last.role !== "assistant" || last.streaming) return [];
  return turnQuestions(last);
}

/** How many of `questions` the draft answers — the carry strip's numerator. */
export function answeredCount(
  questions: readonly AskedQuestion[],
  draft: Readonly<Record<string, string>>,
): number {
  return questions.filter((q) => (draft[q.id] ?? "").trim() !== "").length;
}

/** The draft, narrowed to the open set and cleaned — what rides the send as
 * `ChatRequest.answers`. Ordered as asked, so the turn text below and the clarification
 * blocks the backend appends read in the order the owner answered them. */
export function answerList(
  questions: readonly AskedQuestion[],
  draft: Readonly<Record<string, string>>,
): { question_id: string; answer: string }[] {
  return questions
    .map((q) => ({ question_id: q.id, answer: (draft[q.id] ?? "").trim() }))
    .filter((a) => a.answer !== "");
}

/** What the reply turn SAYS — the client's mirror of `analysis/clarify.owner_turn_text`,
 * so the optimistic user bubble reads exactly as the persisted turn does on reload.
 *
 * Typed text wins outright, as it does server-side: a non-blank message is the free-text
 * degrade path and the answers ride it structurally. An answers-only send renders as the
 * Q/A pairs, and the `Q:`/`A:` labels are a safety boundary rather than formatting — only
 * the `A:` half is the owner's; the `Q:` half is a string a MODEL wrote while reading a
 * note body that may carry someone else's text. */
export function ownerTurnText(
  message: string,
  questions: readonly AskedQuestion[],
  draft: Readonly<Record<string, string>>,
): string {
  if (message.trim() !== "") return message;
  const answered = questions
    .map((q) => ({ q: q.question, a: (draft[q.id] ?? "").trim() }))
    .filter((p) => p.a !== "");
  return answered.map((p) => `Q: ${p.q}\nA: ${p.a}`).join("\n\n");
}

/** The answers a reply turn's own text carries back, keyed by the question they answer.
 *
 * A frozen block reads its answers from here (§3b I9). It is the same rendering
 * `ownerTurnText` writes and `clarify.owner_turn_text` persists, so the pairing is by the
 * exact question string the ask recorded — never by position, which is the mispairing
 * this whole channel is built to refuse. A reply the owner TYPED carries no pairs, and
 * the block then says a row was answered without putting words in their mouth. */
export function answersFromReply(text: string): Map<string, string> {
  const pairs = new Map<string, string>();
  for (const chunk of text.split("\n\n")) {
    const m = /^Q: ([^\n]+)\nA: ([\s\S]+)$/.exec(chunk.trim());
    if (m) pairs.set((m[1] ?? "").trim(), (m[2] ?? "").trim());
  }
  return pairs;
}

/** A frozen block's answers, keyed by question id — the reply turn's own Q/A rendering,
 * paired back by the exact question string the ask recorded. Every id is present, so a
 * question the reply could not be paired to renders as answered-without-words rather
 * than as still open. */
export function sentAnswers(
  questions: readonly AskedQuestion[],
  replyText: string,
): Record<string, string> {
  const pairs = answersFromReply(replyText);
  const out: Record<string, string> = {};
  for (const q of questions) out[q.id] = pairs.get(q.question) ?? "";
  return out;
}
