// The open question set of a note thread, read off the transcript — the data the
// question block renders (AGENT_INGEST_REWRITE §3b I6/I9).
//
// Pure, and reading only what is ALREADY on the wire: the questions are the ask step's
// `args`, which a persisted turn replays exactly as a live one does
// (`useFullBrain.fromTurn`, `transcript.ts`), so a thread reopened weeks later renders
// the same block with no new endpoint and no answer state that lives only in a component.
//
// ⟲ **Those args are the ones the TOOL recorded, and saying so is R3f's third review,
// finding 1.** This comment used to call them "the `ask_owner` call's own recorded args",
// which read as one blob and is two: the ledger row `asktools` writes (which carries the
// server-minted question ids) and the step the transcript persists (the model's raw
// arguments, which carry none — the tool declares no `id` property, so the model never
// sends one). The block therefore fell to `askedQuestions`' positional `q${i+1}` fallback
// and posted ids the open set had never held; `clarify._pair` dropped every tapped answer
// as unknown, so the note received nothing while the frozen block drew them as sent. The
// tool now echoes its recorded args onto the step it streams and the step it persists
// (`ToolResultEvent.args` → `transcript.ts` / `TranscriptAccumulator`), which is what makes
// the sentence above true rather than merely intended.
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

/** Split a candidate list on its top-level separators only.
 *
 * The tool teaches the model to write "Sarah Whitfield (sister, 12 notes), Sarah Chen
 * (work, 3 notes)" (`ask_owner.tool`), so the separator and the detail's own punctuation
 * are the same character. Splitting naively turns two candidates into four, and "12
 * notes)" is not something to offer as an answer.
 *
 * **A SEMICOLON separates too**, which R3f's review found by measuring real model output:
 * "Sarah Whitfield (sister); Sarah Chen (work)" parsed as ONE candidate, so the only
 * tappable thing on the row was a string naming both people — and a tap would have put
 * that whole string into the note as the owner's own answer. The tool asks for commas and
 * the model writes what it writes; a top-level semicolon is never part of a name.
 *
 * **What this deliberately does NOT try to repair is a MALFORMED list** — an unclosed
 * paren swallows the separators after it and the tail parses as one candidate. Every
 * recovery available here (re-splitting on the raw commas, clamping the depth) invents
 * candidates the model never wrote — "4 notes" offered as a person to tap — and a
 * candidate the owner taps is a sentence in his own note. So the parser fails to the
 * honest side and the ESCAPE is what makes that survivable: every candidate row also
 * carries "Something else", which answers in words (§3b I6, R3f's review finding 4). */
function splitCandidates(raw: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const ch of raw) {
    if (ch === "(" || ch === "[") depth += 1;
    else if (ch === ")" || ch === "]") depth = Math.max(0, depth - 1);
    else if ((ch === "," || ch === ";") && depth === 0) {
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
      // The server-minted id the ask recorded. The positional fallback is for the
      // pre-batch ledger shape only (see above and `questions_from_args`) — for a real
      // ask it is the id `clarify._pair` matches against, and a positional stand-in would
      // be dropped as naming no open question.
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

/** What a chunk has to look like to be read back as a Q/A pair — the one shape
 * `answersFromReply` accepts, and therefore the one shape `stripPairLabels` neutralises.
 * Shared by both so the writer's sanitiser and the reader's parser cannot drift apart, and
 * mirrored by `clarify._PAIR_CHUNK`. No `g` flag: it is used with `.test`/`.exec`, which
 * carry `lastIndex` between calls on a global regex. */
const PAIR_CHUNK = /^Q: ([^\n]+)\nA: ([\s\S]+)$/;

/** The typed half, with the channel's own `Q:`/`A:` labels taken off any chunk that would
 * otherwise be read back as a pair — the client's mirror of `clarify._strip_pair_labels`, and it
 * must stay byte-identical to it.
 *
 * The labels are a SAFETY BOUNDARY rather than formatting (see `ownerTurnText`), and a
 * boundary that holds only while the owner does not type the labels himself is not one.
 * The composer is a plain `<textarea>` with no key handling, so Enter inserts a newline
 * and the questions are on screen directly above it — quoting one back is how people
 * reply in a thread:
 *
 *     Q: Which coach?
 *     A: nobody at all
 *
 * lands as its own `\n\n`-separated chunk, matches `answersFromReply`, and the block
 * then shows that question answered in words the backend dropped and told the agent were
 * still open — F1's inverse display, re-created from the other side.
 *
 * ⟲ **It used to take the labels off EVERY line, which deleted words that were the
 * owner's** (R3f's third review, finding 3b): `Two options:` / `A: the cardiologist` /
 * `B: the paediatrician` came back with his `A:` gone and his `B:` kept, an enumerated
 * reply mangled into nonsense — and the backend now runs this same sanitiser on the way
 * to the NOTE, where that is a sentence nobody wrote in his own corpus. So the cut is made
 * only where the forgery is: a chunk that `PAIR_CHUNK` (what `answersFromReply` accepts)
 * would read back as a pair. A chunk that would not is left exactly as typed. A bare `A:`
 * line was never readable as a pair on its own — the reader anchors on the `Q:` — so
 * stripping it bought nothing and cost a word. */
export function stripPairLabels(text: string): string {
  return text
    .split("\n\n")
    .map((chunk) => (PAIR_CHUNK.test(chunk.trim()) ? chunk.replace(/^[ \t]*[QA]: /gm, "") : chunk))
    .join("\n\n");
}

/** What the reply turn SAYS — the client's mirror of `analysis/clarify.owner_turn_text`,
 * so the optimistic user bubble reads exactly as the persisted turn does on reload.
 *
 * A MIXED send renders BOTH halves: the Q/A pairs the block answered, then the words the
 * owner typed beside them. Typed text used to win outright and throw the pairs away, and
 * the turn text is not only prose for the model — it is the transcript's own record of
 * what the owner did, and what the frozen block reads its answers back out of
 * (`answersFromReply`). So the exact send §3b I7 designs (tap two, type a sentence) drew
 * the inverse of what happened: the block said "2 questions · answered" with neither
 * answer shown and the tapped candidate not picked, live and on every reopen, while the
 * note held the opposite — the two answers landed and the typed sentence reached no note.
 *
 * The `Q:`/`A:` labels are a safety boundary rather than formatting — only the `A:` half
 * is the owner's; the `Q:` half is a string a MODEL wrote while reading a note body that
 * may carry someone else's text. The typed half is stripped of those labels
 * (`stripPairLabels`) so it cannot be read back as a pair — a property of the rendering
 * rather than an assumption about what the owner types into a free-text box. */
export function ownerTurnText(
  message: string,
  questions: readonly AskedQuestion[],
  draft: Readonly<Record<string, string>>,
): string {
  const answered = questions
    .map((q) => ({ q: q.question, a: (draft[q.id] ?? "").trim() }))
    .filter((p) => p.a !== "");
  const rendered = answered.map((p) => `Q: ${p.q}\nA: ${p.a}`).join("\n\n");
  // Sanitised HERE, once, so every branch below carries a typed half that cannot forge a
  // pair — including the prose-only one, where the words are the whole turn text.
  const safe = stripPairLabels(message);
  const typed = safe.trim();
  if (rendered && typed) return `${rendered}\n\n${typed}`;
  return rendered || safe;
}

/** One Q/A pair a reply turn's own text carries back. */
export interface ReplyPair {
  question: string;
  answer: string;
}

/** The Q/A pairs a reply turn's own text carries back, IN ORDER.
 *
 * A frozen block reads its answers from here (§3b I9). It is the same rendering
 * `ownerTurnText` writes and `clarify.owner_turn_text` persists, so the pairing is by the
 * exact question string the ask recorded — never by position alone, which is the
 * mispairing this whole channel is built to refuse. A reply the owner TYPED carries no
 * pairs, and the block then says a row was answered without putting words in their mouth;
 * the free text a MIXED send appends after the pairs carries no `Q:`/`A:` labels, so it
 * is not a pair and is not read back as one.
 *
 * A LIST rather than a map, because a question string is not a key: two rows of one set
 * can ask the same words (`ask_owner` does not dedupe them), and a map made both rows
 * replay the second answer. */
export function answersFromReply(text: string): ReplyPair[] {
  const pairs: ReplyPair[] = [];
  for (const chunk of text.split("\n\n")) {
    const m = PAIR_CHUNK.exec(chunk.trim());
    if (m) pairs.push({ question: (m[1] ?? "").trim(), answer: (m[2] ?? "").trim() });
  }
  return pairs;
}

/** What a settled reply DID to one question of the open set.
 *
 * THREE outcomes and not two, which is R3f's second review, finding 1. `sentAnswers`
 * collapsed the last two into `""`, and the block read `""` as "answered in your reply":
 * on the partial send §3b I7 designs — one candidate tapped, two rows left blank, an
 * aside typed — it told the owner that the two rows it had left OPEN were answered
 * somewhere in his reply, live and on every reopen, while `owner_reply_notice` told the
 * agent they were open and the agent's next turn asked them again. The screen said you
 * answered it and the agent asked again, on the one screen the owner has. */
export type SentOutcome =
  /** Its words are on the reply turn, paired to it by the exact question string. */
  | { kind: "paired"; answer: string }
  /** The reply was PROSE ALONE, and this is the question the backend paired it with —
   * `clarify._pair`'s degrade rule, "free text alone answers the OLDEST open question".
   * The words are the whole turn text rather than this row's, so the row says where they
   * are instead of putting them in the owner's mouth. */
  | { kind: "in-reply" }
  /** Nothing paired it. It is still open, the agent was told so (`owner_reply_notice`),
   * and it may be re-asked on the next turn. */
  | { kind: "open" };

/** What a settled reply did to each question of the set, keyed by QUESTION ID — read out
 * of the reply turn's own Q/A rendering (§3b I9), paired by the exact question string the
 * ask recorded and never by position alone.
 *
 * A pair is CONSUMED once it is claimed, so two questions worded identically take the
 * first and the second rendering rather than both taking the last. Both sides walk the
 * open set in its asked order — `ownerTurnText` and `clarify.owner_turn_text` render in
 * that order, this reads in it — so the n-th same-worded row gets the n-th answer, which
 * is the one it was given.
 *
 * The `in-reply` rule MIRRORS the backend rather than guessing: prose beside any
 * structured answer is an aside `_pair` files nowhere (F5), so a reply that carries pairs
 * leaves every unpaired row plainly open; prose ALONE has exactly one thing it could be
 * answering, and `_pair` gives it the oldest open question — the first of this set. The
 * one reply it can still over-claim is prose the backend refused to file at all (a thread
 * that was no longer waiting, a lost claim), which leaves no trace on the wire for any
 * client to read. */
export function sentOutcomes(
  questions: readonly AskedQuestion[],
  replyText: string,
): Record<string, SentOutcome> {
  const pairs = answersFromReply(replyText);
  const proseOnly = pairs.length === 0 && replyText.trim() !== "";
  const claimed = new Set<number>();
  const out: Record<string, SentOutcome> = {};
  questions.forEach((q, n) => {
    const i = pairs.findIndex((p, j) => !claimed.has(j) && p.question === q.question);
    if (i >= 0) {
      claimed.add(i);
      out[q.id] = { kind: "paired", answer: pairs[i]?.answer ?? "" };
    } else {
      out[q.id] = proseOnly && n === 0 ? { kind: "in-reply" } : { kind: "open" };
    }
  });
  return out;
}

/** A frozen block's answers, keyed by QUESTION ID — the WORDS of `sentOutcomes`, `""`
 * where the reply carried none. Kept as its own reading because that is what the
 * candidate rows compare against to draw a pick; which rows are still open is the
 * outcome's job, not this one's. */
export function sentAnswers(
  questions: readonly AskedQuestion[],
  replyText: string,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [id, o] of Object.entries(sentOutcomes(questions, replyText))) {
    out[id] = o.kind === "paired" ? o.answer : "";
  }
  return out;
}
