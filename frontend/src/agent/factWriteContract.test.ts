// The D3 write rung, driven by the BACKEND'S OWN JSON.
//
// Shared with the backend (tests/unit/test_fact_write_contract.py). The `event` blobs in
// testdata/fact_write_contract.json are real `ToolResultEvent.model_dump(mode="json")`
// output, and that test asserts the backend still emits exactly them — so this file is
// the only place the rung is exercised on the wire it actually receives.
//
// It exists because the rung shipped reading six field names that existed on NEITHER side
// (`status`, `predicate`, `qualifier`, `value`, `replaced`, `from_attachment`) plus a
// `truncated` that existed nowhere, and every test on both sides was green — because every
// test on both sides built its own input (`entityWrites.test.ts`'s `fact()` helper is
// still the right tool for wording cases; it just cannot catch a wire mismatch). Fed the
// real payload, `tallyWrites` fell through `else tally.written += 1` for every undefined
// status and reported a HELD fact — one `decide()` refused to make live — as written.
//
// The path here is the shipped one end to end: `applyEvent` folds the event onto a live
// turn exactly as the SSE stream does, `toolStep` projects it, and the pure reducers say
// what the owner reads.

import { describe, expect, it } from "vitest";
// Imported, not read via node:fs, so the test typechecks without node types in this
// browser project — the same arrangement `format.test.ts` uses for its parity fixture.
import contract from "../../../testdata/fact_write_contract.json";
import { stepWriteState, writePhrase } from "./entityWrites";
import { toolStep } from "./toolSummary";
import { applyEvent, streamingAssistant } from "./transcript";
import type { ChatEvent, FactWrite } from "./types";

interface Expectation {
  tool: string;
  state: string;
  phrase: string;
}

const cases = contract.cases as unknown as {
  name: string;
  event: ChatEvent;
  expect: Expectation;
}[];

/** The step as the PWA builds it: open a streaming turn, announce the call, fold the
 * backend's result event onto it, project. No shortcut past `applyEvent` — the fold is
 * where a field the reducer never copies would go missing. */
function stepFor(event: ChatEvent, tool: string) {
  const call = event as { tool_call_id: string };
  let messages = [streamingAssistant()];
  messages = applyEvent(messages, {
    type: "tool_call",
    id: call.tool_call_id,
    name: tool,
    arguments: {},
  });
  messages = applyEvent(messages, event);
  const activity = messages[messages.length - 1]?.tools?.[0];
  if (activity === undefined) throw new Error("the fold dropped the tool call entirely");
  return toolStep(activity);
}

describe("the D3 rung, on the backend's real payload", () => {
  for (const c of cases) {
    it(c.name, () => {
      const step = stepFor(c.event, c.expect.tool);
      expect(stepWriteState(step)).toBe(c.expect.state);
      expect(writePhrase(step)).toBe(c.expect.phrase);
    });
  }

  it("carries every per-fact field the expanded rung renders", () => {
    // The six names the rung reads, checked as they ARRIVE rather than as a fixture
    // spells them — a rename on either side lands here.
    const step = stepFor(cases[0]?.event as ChatEvent, "assert_fact");
    const [replaced, held] = step.facts;
    expect(replaced?.status).toBe("replaced");
    expect(replaced?.predicate).toBe("lives_in");
    expect(replaced?.value).toBe("Marina District");
    // `ClaimDiffView` renders only when this is present — it was unreachable before,
    // which made extracting the shared diff renderer pointless.
    expect(replaced?.replaced).toBe("Jeff lives in Noe Valley");
    expect(held?.status).toBe("held");
    expect(held?.domain).toBe("health");

    const photo = stepFor(cases[1]?.event as ChatEvent, "assert_fact");
    // D12 had no producer at all: `from_attachment` appeared nowhere in the write path.
    expect(photo.facts[0]?.from_attachment).toBe(true);
    expect(photo.facts[0]?.qualifier).toBe("a1c");

    expect(stepFor(cases[2]?.event as ChatEvent, "assert_fact").truncated).toBe(true);
  });

  it("reads a held fact as held on a turn persisted before `status` existed", () => {
    // Such a turn carries `outcome` alone. The frontend mirrors the backend's reduction
    // table for exactly this case, and the two agree on the fixture above ("every outcome
    // word the write path can report") — so the mirror cannot drift unnoticed.
    const legacy = contract.persisted_before_status as unknown as {
      tool: string;
      facts: FactWrite[];
      expect: Expectation;
    };
    const step = stepFor(
      {
        type: "tool_result",
        tool_call_id: "legacy",
        ok: true,
        summary: "",
        facts: legacy.facts,
      } as ChatEvent,
      legacy.tool,
    );
    expect(stepWriteState(step)).toBe(legacy.expect.state);
    expect(writePhrase(step)).toBe(legacy.expect.phrase);
  });
});
