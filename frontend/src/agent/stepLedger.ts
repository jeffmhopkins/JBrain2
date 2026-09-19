// The Worked row's right-hand side — what CAME BACK, in one phrase.
//
// A row has always carried what was ASKED (`Searched your notes · "refinance closing"`)
// and, for four tools, what came back. Every other row ended at the question, which makes
// the strip a record of how hard the agent worked rather than of what it found — and a row
// with nothing on its right is a row you cannot check.
//
// This is the one place that decides, so the four hardcoded per-tool branches in `StepRow`
// become one call (docs/archive/SHOW_THE_WORKING_PLAN.md W1). The order below is the whole
// design: a tool that AUTHORED its answer wins, because only the handler knows which part
// of its own result was the answer; everything else is read off the structured fields the
// step already carries. Nothing here parses `summary`, which is model-facing text.

import { entityPhrase, stepWriteState, writePhrase } from "./entityWrites";
import type { ToolStep } from "./toolSummary";

export interface Ledger {
  text: string;
  /** The class the phrase reads in — a name, never a colour (DESIGN.md): the stylesheet
   * owns the palette. The write and resolve phrases keep the `fbw-*` classes they already
   * shipped with, so their held/failed/already colouring is unchanged. */
  cls: string;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export function stepLedger(step: ToolStep): Ledger | undefined {
  // Writes first, and unchanged: `writePhrase` already owns the seven D3 states, including
  // its own in-flight and failed wordings, and a write's outcome is more specific than
  // anything below could say about it.
  const write = writePhrase(step);
  if (write !== undefined) return { text: write, cls: `fbw-cnt fbw-${stepWriteState(step)}` };

  const entities = entityPhrase(step);
  if (entities !== undefined) return { text: entities, cls: "fbw-cnt fbw-ents" };

  // One rule for all 127 tools rather than a per-tool failure string. The status dot
  // already says something went wrong; this says it in the column the eye is scanning,
  // which is what makes a failed call findable in a strip of twelve.
  if (step.ok === false) return { text: "failed", cls: "fbl-bad" };

  // In flight: the mark is the live signal, and a half-finished answer is worse than none.
  if (step.ok === undefined) return undefined;

  // The handler's own answer. It wins over every count below, because a count is what the
  // client could work out and this is what only the tool knew.
  if (step.result) return { text: step.result, cls: "fbl-res" };

  if (step.sources.length > 0) return { text: plural(step.sources.length, "note"), cls: "" };
  if (step.webSources.length > 0)
    return { text: plural(step.webSources.length, "result"), cls: "" };
  return undefined;
}
