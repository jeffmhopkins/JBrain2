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
  | "held";

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
  /** D12 — writes that came from an attachment rather than the note's own prose. */
  fromPhoto: number;
}

export function tallyWrites(facts: readonly FactWrite[]): WriteTally {
  const tally: WriteTally = { written: 0, replaced: 0, held: 0, fromPhoto: 0 };
  for (const f of facts) {
    const status = factStatus(f);
    if (status === "replaced") tally.replaced += 1;
    else if (status === "held") tally.held += 1;
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
  if (tally.replaced > 0 && tally.written === 0 && tally.held === 0) return "replaced";
  if (tally.held > 0 && tally.written === 0 && tally.replaced === 0) return "held";
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
    return step.truncated ? "nothing written · truncated" : "nothing written";
  const tally = tallyWrites(step.facts);
  const parts: string[] = [];
  if (tally.written > 0) parts.push(plural(tally.written, "written"));
  if (tally.replaced > 0) parts.push(plural(tally.replaced, "replaced"));
  if (tally.held > 0) parts.push(plural(tally.held, "held"));
  // The domain is part of the headline, not only of the expanded detail: a health write
  // has to be legible as a health write without a tap.
  parts.push(writeDomains(step.facts).join(" + "));
  if (tally.fromPhoto > 0) parts.push("from a photo");
  if (step.truncated) parts.push("truncated");
  return parts.join(" · ");
}

/** The verb for ONE write, as its chip reads. */
export function writeVerb(fact: FactWrite): string {
  return factStatus(fact);
}
