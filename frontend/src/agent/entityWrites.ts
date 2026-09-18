// What a tool call did to the owner's graph, reduced to the seven states D3 of
// docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md names — `written · replaced · held ·
// from a photo · failed · truncated · writing…`. Pure, so the wording is unit-testable
// away from the surface that renders it.
//
// The source is the STEP, never a second fetch: `FactWrite`s ride the tool result and
// are persisted onto the turn, so a conversation reopened days later reduces to exactly
// the phrase it showed live. The `note_conversation_tool_calls` ledger is deliberately
// NOT read here — it stores `fact_ids` with no status and no statement, so it can say
// that a call wrote and which domains it touched, never which of these seven states a
// write reached (see the module's tests and the plan's D3 notes).

import type { ToolStep } from "./toolSummary";
import type { Domain, FactWrite, WriteStatus } from "./types";

/** The state a whole step reached. `nothing` is a write tool that wrote nothing — a
 * real, different statement from a tool that never writes (which reports `none`). */
export type StepWriteState =
  | "none"
  | "writing"
  | "failed"
  | "nothing"
  | "written"
  | "replaced"
  | "held"
  | "already";

/** The tools whose steps are graph writes. A step of any other tool has no write rung
 * at all, so a read tool never grows an empty "wrote nothing" line. Kept as a name set
 * rather than inferred from `facts.length`, because "this write tool wrote nothing" is
 * exactly the case that has to render. */
export const WRITE_TOOLS: ReadonlySet<string> = new Set([
  "assert_fact",
  "resolve_entity",
  // D11's owner correction: it force-supersedes and pins through the same
  // `commit_facts`, so a correction that wrote NOTHING has to say so too — without
  // this it renders `none`, which reads as "not a write tool" rather than "it failed
  // to change anything", the one outcome the owner most needs to see.
  "correct_fact",
  // The whole-note reading (AGENT_INGEST_REWRITE R1) — the verb that actually writes a
  // note's graph, and the one omitted here until the owner reported that he could not
  // tell his facts had landed. Without it `stepWriteState` returned `none` for every
  // `close_reading` that wrote nothing, so the one call responsible for his entities
  // rendered as "not a write tool" — the exact misreading the `correct_fact` line above
  // was added to prevent.
  "close_reading",
]);

/** The domain of a write, NAMED — D3's "never colour alone" is an accessibility and
 * firewall-legibility rule, not a style note, so this is the one place a domain becomes
 * text and every renderer goes through it. An unrecognised code says so rather than
 * silently rendering as a bare dot. */
export function domainWord(domain: string): string {
  const known: Record<Domain, string> = {
    general: "general",
    health: "health",
    finance: "finance",
    location: "location",
  };
  return known[domain as Domain] ?? "unknown domain";
}

/** The distinct domains a step's writes landed in, in a stable order (the firewall
 * order the rest of the app lists them in), so the same step reads the same way twice. */
const DOMAIN_ORDER: readonly Domain[] = ["general", "health", "finance", "location"];

export function writeDomains(facts: readonly FactWrite[]): string[] {
  const present = new Set(facts.map((f) => f.domain as string));
  const ordered = DOMAIN_ORDER.filter((d) => present.has(d)).map((d) => d as string);
  const unknown = [...present].filter((d) => !DOMAIN_ORDER.includes(d as Domain)).sort();
  return [...ordered, ...unknown].map(domainWord);
}

/** The write path's seven-word outcome vocabulary, reduced to D3's three states —
 * the SAME table as `contracts._WRITE_STATUS` on the backend, which is where a live
 * event's `status` is computed. It is mirrored here for one case only: a turn
 * PERSISTED before `status` existed carries `outcome` alone, and reading such a fact
 * as "written" is precisely the bug this rung exists to prevent.
 * `factsFromEvent.test.ts` asserts the two tables agree on real backend output. */
const OUTCOME_STATUS: Readonly<Record<string, WriteStatus>> = {
  written: "written",
  already: "written",
  closed: "written",
  historical: "written",
  promoted: "written",
  replaced: "replaced",
  held: "held",
};

/** One fact's D3 state. `status` when the server sent it, else the same reduction of
 * `outcome`, else `held`.
 *
 * The final fallback is deliberately NOT "written". A fact whose state cannot be
 * established is a fact nothing has said is live, and the two errors are not
 * symmetric: showing a live fact as held understates what happened and the owner can
 * see it is wrong from the graph; showing a HELD fact as written tells them the graph
 * says something it does not — the single failure `ask_owner` and the persona prompt
 * both exist to surface. An unreadable write fails towards the honest answer. */
export function factStatus(fact: FactWrite): WriteStatus {
  if (fact.status !== undefined) return fact.status;
  if (fact.outcome !== undefined) return OUTCOME_STATUS[fact.outcome] ?? "held";
  return "held";
}

export interface WriteTally {
  written: number;
  replaced: number;
  held: number;
  /** Facts this call found ALREADY on file and left alone. Counted apart from `written`
   * — the contract folds them together (`factStatus`), the screen must not. */
  already: number;
  /** D12 — writes that came from an attachment rather than the note's own prose. */
  fromPhoto: number;
}

export function tallyWrites(facts: readonly FactWrite[]): WriteTally {
  const tally: WriteTally = { written: 0, replaced: 0, held: 0, already: 0, fromPhoto: 0 };
  for (const f of facts) {
    const face = writeVerb(f);
    if (face === "replaced") tally.replaced += 1;
    else if (face === "held") tally.held += 1;
    else if (face === "already") tally.already += 1;
    else tally.written += 1;
    if (f.from_attachment) tally.fromPhoto += 1;
  }
  return tally;
}

/** The step's headline state, for the mark on the collapsed row. Liveness and failure
 * outrank the writes: a call still in flight has not written anything yet, and a failed
 * one wrote nothing whatever its arguments claimed. */
export function stepWriteState(step: ToolStep): StepWriteState {
  if (!WRITE_TOOLS.has(step.name) && step.facts.length === 0) return "none";
  if (step.ok === undefined) return "writing";
  if (step.ok === false) return "failed";
  // A resolve that minted entities and no facts did NOT write nothing — the shipped
  // entity chips are what it wrote, and claiming otherwise over them would be false.
  if (step.facts.length === 0 && step.entities.length > 0) return "none";
  if (step.facts.length === 0) return "nothing";
  const tally = tallyWrites(step.facts);
  // A call that changed NOTHING — every fact of it already on file — is the ordinary
  // shape of a re-reading, and it gets its own headline so it stops reading as a write.
  if (tally.already > 0 && tally.written + tally.replaced + tally.held === 0) return "already";
  if (tally.replaced > 0 && tally.written + tally.held + tally.already === 0) return "replaced";
  if (tally.held > 0 && tally.written + tally.replaced + tally.already === 0) return "held";
  return "written";
}

function plural(n: number, verb: string): string {
  return `${n} ${verb}`;
}

/** One line naming what the call did, for the collapsed row — the seven D3 states in
 * the owner's words. Returns undefined for a step that is not a write, so nothing is
 * added to a search or a read row. */
export function writePhrase(step: ToolStep): string | undefined {
  const state = stepWriteState(step);
  if (state === "none") return undefined;
  if (state === "writing") return "writing…";
  if (state === "failed") return step.truncated ? "failed · truncated" : "failed";
  if (state === "nothing")
    return step.truncated ? "nothing recorded · truncated" : "nothing recorded";
  const tally = tallyWrites(step.facts);
  const parts: string[] = [];
  if (tally.written > 0) parts.push(plural(tally.written, "recorded"));
  if (tally.replaced > 0) parts.push(plural(tally.replaced, "updated"));
  if (tally.held > 0) parts.push(plural(tally.held, "not recorded"));
  if (tally.already > 0) parts.push(plural(tally.already, "already on file"));
  // The domain is part of the headline, not only of the expanded detail: a health write
  // has to be legible as a health write without a tap.
  parts.push(writeDomains(step.facts).join(" + "));
  if (tally.fromPhoto > 0) parts.push("from a photo");
  if (step.truncated) parts.push("truncated");
  return parts.join(" · ");
}

/** What a whole TURN did to the graph, for the collapsed chip — "recorded 2 facts",
 * "updated 1 fact". A turn that changed the owner's database must not be summarised by
 * a step count: the count is how hard the agent worked, and what he is looking for is
 * whether anything landed. Undefined when the turn wrote nothing at all, so a pure
 * read turn keeps the plain step count it has always had. */
export function turnWriteSummary(steps: readonly ToolStep[]): string | undefined {
  const facts = steps
    .filter((s) => WRITE_TOOLS.has(s.name) && s.ok !== false)
    .flatMap((s) => s.facts);
  if (facts.length === 0) return undefined;
  let recorded = 0;
  let updated = 0;
  let notRecorded = 0;
  let reconfirmed = 0;
  for (const fact of facts) {
    // The RAW outcome, not `factStatus`'s reduction. `OUTCOME_STATUS` folds `already`,
    // `closed` and `historical` into `written`, which is right for the expanded rung —
    // each of those IS on file — and wrong for this headline. A re-reading restates the
    // WHOLE note (`analysis/converse.py`), so on the pass after the owner corrects one
    // value, every other fact comes back `already` and the reduction would announce
    // "recorded 12 facts" for a turn that changed one. What this line is for is whether
    // anything landed; a fact that was already on file, unchanged, is not news.
    const outcome = fact.outcome ?? fact.status;
    if (outcome === "replaced") updated += 1;
    else if (outcome === "held") notRecorded += 1;
    else if (outcome === "already") reconfirmed += 1;
    else if (outcome === "written" || outcome === "promoted") recorded += 1;
  }
  const parts: string[] = [];
  const noun = (n: number): string => `${n} fact${n === 1 ? "" : "s"}`;
  if (recorded > 0) parts.push(`recorded ${noun(recorded)}`);
  if (updated > 0) parts.push(`updated ${noun(updated)}`);
  // `writeWord`, not a fourth spelling of the same state: the collapsed chip said "held"
  // while expanding the same turn said "not recorded".
  if (notRecorded > 0) parts.push(`${writeWord("held")} ${noun(notRecorded)}`);
  // A pass that changed nothing has an ANSWER, and it is not silence. Falling through to
  // the step count here left the owner's second reading of a note headlined "1 step",
  // which tells him neither that it ran nor that it agreed with what was already there.
  // Said out loud, a re-reading that confirms the note is a result he can accept.
  if (parts.length === 0) {
    return reconfirmed > 0 ? `nothing new · ${noun(reconfirmed)} re-confirmed` : undefined;
  }
  // Alongside real changes the re-confirmations are the tail, not the news — they say
  // what the pass did with the rest of the note, so "updated 1 fact" cannot be read as
  // "the other eleven are gone".
  if (reconfirmed > 0) parts.push(`${noun(reconfirmed)} unchanged`);
  return parts.join(" · ");
}

/** What a resolve did to the owner's cast, for the collapsed row — "2 new · 1 already
 * known". `stepWriteState` calls a mint-only step `none` on purpose (it wrote no FACT,
 * and claiming it wrote nothing over the entities it shipped would be false), and the
 * cost of that was a `resolve_entity` row with no mark at all: the one call that creates
 * the owner's records read as a call that did nothing. This is that row's headline.
 *
 * Undefined for a step that resolved no entities, and for one whose refs predate
 * `created` — a count of entities with no answer to "new or already mine?" is the step
 * count again, in a costlier form. */
export function entityPhrase(step: ToolStep): string | undefined {
  if (step.entities.length === 0 || step.ok !== true) return undefined;
  const known = step.entities.filter((e) => e.created !== undefined);
  if (known.length === 0) return undefined;
  const made = known.filter((e) => e.created === true).length;
  const matched = known.length - made;
  const parts: string[] = [];
  if (made > 0) parts.push(`${made} new`);
  if (matched > 0) parts.push(`${matched} already known`);
  return parts.join(" · ");
}

/** What the owner is shown a write BECAME — the three contract states plus `already`.
 *
 * `factStatus` stays the contract reduction and must: it mirrors `contracts._WRITE_STATUS`,
 * and `already` IS `written` there, because the fact is on file either way. On screen the
 * two are not the same event at all. A re-reading restates the whole note, so after the
 * owner corrects one value every other fact comes back `already`, and rendering those as
 * "recorded" told him the box had just written twelve facts it had in fact only re-read.
 * The face is where that distinction lives; the status is where the graph's is. */
export type WriteFace = WriteStatus | "already";

export function writeVerb(fact: FactWrite): WriteFace {
  if (fact.outcome === "already") return "already";
  return factStatus(fact);
}

/** The owner's word for a write state. `WriteStatus` is the write path's OWN vocabulary
 * and stays exactly as it is — it mirrors `contracts._WRITE_STATUS` and a rename there
 * would be a contract change. This is the rendering, and it is separate because the two
 * audiences are: "written" is what the writer did, "recorded" is what the owner asked
 * the box for; "replaced" reads as deletion when the old row is in fact kept as history,
 * so the write says "updated" and the diff says where the old value went. */
const WRITE_WORD: Record<string, string> = {
  written: "recorded",
  replaced: "updated",
  held: "not recorded",
  // Not "recorded": this pass wrote nothing here, it found the value already on file and
  // left it. Saying "recorded" over it is the sentence that made a re-reading look like
  // twelve new writes.
  already: "already on file",
};

export function writeWord(status: string): string {
  return WRITE_WORD[status] ?? status;
}

/** One line of the turn's ledger — what changed, in the owner's words. */
export interface LedgerRow {
  key: string;
  /** The sentence itself: a fact's statement, or a newly created entity's name. */
  text: string;
  /** `new` is an entity this turn MINTED, which is a change to his graph with no fact
   * of its own — the record now exists. The rest are a write's face. */
  face: WriteFace | "new";
  domain: string;
}

/** What a turn CHANGED, as lines, for the card that renders on the face of the turn
 * rather than two taps inside it (DESIGN.md, "the ledger is not a disclosure").
 *
 * Only changes. A fact already on file is what a re-reading is mostly made of, and a
 * ledger that listed all of them would bury the one line that is news under eleven that
 * are not — the count of those belongs in the turn's summary (`turnWriteSummary`), which
 * is where it says "nothing new" when there is none.
 *
 * A step still in flight or failed contributes nothing: neither has a settled answer to
 * what changed, and a line that appears and then retracts is worse than one that arrives
 * a second late. */
export function ledgerRows(steps: readonly ToolStep[]): LedgerRow[] {
  const rows: LedgerRow[] = [];
  // ACROSS steps, not only within one. `toolStep` already gives each step its entities
  // once; a minted entity is then reported AGAIN by every later step that touches it,
  // because `created` belongs to the handle and the handle lives for the whole pass. So a
  // note introducing the owner's dog resolves `Me` and `Boss` (two rows) and then names
  // them in `close_reading` (two more) — and on a LEDGER_CAP of four, the one row that is
  // actually the receipt, the fact itself, was pushed behind "+N more": back behind the
  // tap this card exists to remove, on the very first pass, which is the pass he reported.
  const minted = new Set<string>();
  for (const step of steps) {
    if (step.ok !== true) continue;
    for (const e of step.entities) {
      if (e.created === true && !minted.has(e.entity_id)) {
        minted.add(e.entity_id);
        rows.push({ key: `e:${e.entity_id}`, text: e.label, face: "new", domain: e.domain });
      }
    }
    if (!WRITE_TOOLS.has(step.name)) continue;
    for (const f of step.facts) {
      const face = writeVerb(f);
      if (face === "already") continue;
      rows.push({ key: `f:${f.fact_id}`, text: f.label, face, domain: f.domain });
    }
  }
  return rows;
}

/** The word a ledger row's face reads as. `new` is the entity case and says what
 * happened rather than naming the state — "added" is a record that did not exist. */
export function ledgerWord(face: LedgerRow["face"]): string {
  return face === "new" ? "added" : writeWord(face);
}
