// The Worked row's right-hand side. The ORDER is the design, so it is what is tested:
// an authored answer beats a count, a write keeps its own D3 wording, and a failed call
// says so for every tool rather than for the four that used to be named in StepRow.

import { describe, expect, it } from "vitest";
import { stepLedger } from "./stepLedger";
import type { ToolStep } from "./toolSummary";

function step(over: Partial<ToolStep> & { name: string }): ToolStep {
  return {
    id: "c1",
    ok: true,
    label: "Did a thing",
    inline: undefined,
    sources: [],
    webSources: [],
    entities: [],
    facts: [],
    truncated: false,
    args: undefined,
    summary: undefined,
    result: undefined,
    ...over,
  };
}

const NOTE = { noteId: "n1", domain: "general", text: "…" };

describe("what came back", () => {
  it("a tool that authored its answer shows the answer", () => {
    const s = step({ name: "calculate", result: "2568" });
    expect(stepLedger(s)).toEqual({ text: "2568", cls: "fbl-res" });
  });

  it("the authored answer beats a count the client could have worked out itself", () => {
    // The whole reason the field is handler-authored: only the tool knew this.
    const s = step({ name: "read_note", result: "5.15% · 30yr", sources: [NOTE] });
    expect(stepLedger(s)?.text).toBe("5.15% · 30yr");
  });

  it("falls back to counting what the step structurally carries", () => {
    expect(stepLedger(step({ name: "search", sources: [NOTE] }))).toEqual({
      text: "1 note",
      cls: "",
    });
    expect(stepLedger(step({ name: "search", sources: [NOTE, NOTE, NOTE] }))?.text).toBe("3 notes");
  });

  it("counts web pages as results, not notes — a page is not an owner note", () => {
    const web = { url: "https://x.example", title: "A page" };
    expect(stepLedger(step({ name: "web_search", webSources: [web, web] }))?.text).toBe(
      "2 results",
    );
  });

  it("a failed call says so, whatever the tool", () => {
    // Previously invisible in this column for every tool but the writes: the dot said it
    // and nothing in the scanned column did.
    expect(stepLedger(step({ name: "get_time", ok: false }))).toEqual({
      text: "failed",
      cls: "fbl-bad",
    });
  });

  it("a failed call that authored an answer still reads as failed", () => {
    const s = step({ name: "run_python", ok: false, result: "no output" });
    expect(stepLedger(s)?.text).toBe("failed");
  });

  it("an in-flight call shows nothing — a half-finished answer is worse than none", () => {
    expect(stepLedger(step({ name: "search", ok: undefined, sources: [NOTE] }))).toBeUndefined();
  });

  it("a tool with nothing to report leaves the column empty rather than inventing a phrase", () => {
    expect(stepLedger(step({ name: "get_time" }))).toBeUndefined();
  });

  it("a write keeps its own D3 wording and its state class", () => {
    const s = step({
      name: "assert_fact",
      facts: [{ fact_id: "f1", label: "x", domain: "general", outcome: "written" }],
    });
    const ledger = stepLedger(s);
    expect(ledger?.text).toContain("1 recorded");
    expect(ledger?.cls).toBe("fbw-cnt fbw-written");
  });
});
