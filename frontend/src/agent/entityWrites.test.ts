import { describe, expect, it } from "vitest";
import {
  domainWord,
  stepWriteState,
  tallyWrites,
  turnWriteSummary,
  writeDomains,
  writePhrase,
} from "./entityWrites";
import type { ToolStep } from "./toolSummary";
import type { FactWrite } from "./types";

function fact(over: Partial<FactWrite> = {}): FactWrite {
  const base: FactWrite = {
    fact_id: over.fact_id ?? `f${Math.random()}`,
    label: "Me lives_in Marina District",
    domain: "general",
    status: "written",
    ...over,
  };
  // A turn persisted by the write path carries the RAW outcome too, and the two can
  // disagree in the one direction that matters: `outcome: "already"` reduces to
  // `status: "written"`. A case that sets `outcome` is asking for that shape, so the
  // default `status` is dropped rather than left to contradict it.
  if (over.outcome !== undefined && over.status === undefined) {
    const { status: _status, ...rest } = base;
    return rest as FactWrite;
  }
  return base;
}

function step(over: Partial<ToolStep> & { name: string }): ToolStep {
  return {
    id: "c1",
    ok: true,
    label: "Recorded what the note says",
    inline: undefined,
    sources: [],
    webSources: [],
    entities: [],
    facts: [],
    truncated: false,
    args: undefined,
    summary: undefined,
    ...over,
  };
}

describe("the seven D3 states", () => {
  it("a call still in flight is writing…, not 'wrote nothing'", () => {
    const s = step({ name: "assert_fact", ok: undefined });
    expect(stepWriteState(s)).toBe("writing");
    expect(writePhrase(s)).toBe("writing…");
  });

  it("a failed call reports failed whatever its arguments claimed", () => {
    const s = step({ name: "assert_fact", ok: false, facts: [fact()] });
    expect(stepWriteState(s)).toBe("failed");
    expect(writePhrase(s)).toBe("failed");
  });

  it("a write tool that wrote nothing says so — silence would read as 'not a write'", () => {
    const s = step({ name: "assert_fact", facts: [] });
    expect(stepWriteState(s)).toBe("nothing");
    expect(writePhrase(s)).toBe("nothing recorded");
  });

  it("close_reading is a write tool — a reading that recorded nothing has to say so", () => {
    // The verb that actually writes a note's graph. It was missing from `WRITE_TOOLS`,
    // so this step reported `none` — "not a write tool" — and the owner, looking at the
    // one call responsible for his entities, was shown no write rung at all and a step
    // labelled "Read the whole note". He reported it as "I don't see how it actually
    // added the entity to the database".
    const s = step({ name: "close_reading", facts: [] });
    expect(stepWriteState(s)).toBe("nothing");
    expect(writePhrase(s)).toBe("nothing recorded");
  });

  it("a resolve that minted entities and no facts leaves the chips to speak", () => {
    const s = step({
      name: "resolve_entity",
      entities: [{ kind: "entity", entity_id: "e1", label: "Priya", domain: "general" }],
    });
    expect(stepWriteState(s)).toBe("none");
    expect(writePhrase(s)).toBeUndefined();
  });

  it("a read tool gets no write phrase at all", () => {
    expect(writePhrase(step({ name: "search" }))).toBeUndefined();
    expect(stepWriteState(step({ name: "search" }))).toBe("none");
  });

  it("names written, replaced and held, each with its count", () => {
    const s = step({
      name: "assert_fact",
      facts: [
        fact({ fact_id: "a" }),
        fact({ fact_id: "b" }),
        fact({ fact_id: "c", status: "replaced", replaced: "Sunset District" }),
        fact({ fact_id: "d", status: "held" }),
      ],
    });
    expect(writePhrase(s)).toBe("2 recorded · 1 updated · 1 not recorded · general");
    expect(tallyWrites(s.facts)).toEqual({ written: 2, replaced: 1, held: 1, fromPhoto: 0 });
  });

  it("marks an attachment-sourced write as from a photo (D12)", () => {
    const s = step({
      name: "assert_fact",
      facts: [fact({ from_attachment: true, domain: "health" })],
    });
    expect(writePhrase(s)).toBe("1 recorded · health · from a photo");
  });

  it("says truncated, so a partial batch is never shown as a whole one", () => {
    const s = step({ name: "assert_fact", facts: [fact()], truncated: true });
    expect(writePhrase(s)).toBe("1 recorded · general · truncated");
    // And on the two states that carry no write list either.
    expect(writePhrase(step({ name: "assert_fact", ok: false, truncated: true }))).toBe(
      "failed · truncated",
    );
    expect(writePhrase(step({ name: "assert_fact", truncated: true }))).toBe(
      "nothing recorded · truncated",
    );
  });
});

describe("the domain is named, never colour alone", () => {
  it("every knowledge domain has a word", () => {
    expect(domainWord("health")).toBe("health");
    expect(domainWord("finance")).toBe("finance");
    expect(domainWord("location")).toBe("location");
    expect(domainWord("general")).toBe("general");
  });

  it("an unrecognised code says so rather than degrading to a bare dot", () => {
    expect(domainWord("jmolt")).toBe("unknown domain");
  });

  it("a mixed-domain call names both, in the firewall's order", () => {
    const facts = [fact({ domain: "health" }), fact({ domain: "general" })];
    expect(writeDomains(facts)).toEqual(["general", "health"]);
  });

  it("a health write is legible as a health write without expanding the step", () => {
    const s = step({ name: "assert_fact", facts: [fact({ domain: "health" })] });
    expect(writePhrase(s)).toContain("health");
  });
});

describe("what a whole turn did to the graph", () => {
  it("names what landed, so a turn that wrote is never summarised as a step count", () => {
    const steps = [
      step({ name: "resolve_entity", entities: [] }),
      step({
        name: "close_reading",
        facts: [fact({ status: "replaced", replaced: 'My tv is 58".' })],
      }),
    ];
    expect(turnWriteSummary(steps)).toBe("updated 1 fact");
  });

  it("does not call a re-reading's unchanged facts 'recorded'", () => {
    // The trap a review caught. `OUTCOME_STATUS` folds `already` into `written`, which is
    // right for the expanded rung — the fact IS on file — and wrong for this headline.
    // A re-reading restates the WHOLE note, so on the pass after the owner corrects one
    // value every other fact comes back `already`, and the reduction would announce
    // "recorded 12 facts" for a turn that changed one.
    const steps = [
      step({
        name: "close_reading",
        facts: [
          fact({ outcome: "already" }),
          fact({ outcome: "already" }),
          fact({ outcome: "replaced", replaced: 'My tv is 58".' }),
        ],
      }),
    ];
    expect(turnWriteSummary(steps)).toBe("updated 1 fact");
  });

  it("says nothing at all when a re-reading changed nothing", () => {
    // The step count is then the honest headline. A turn that restated the note without
    // altering it must not claim otherwise.
    const steps = [step({ name: "close_reading", facts: [fact({ outcome: "already" })] })];
    expect(turnWriteSummary(steps)).toBeUndefined();
  });

  it("speaks the same word the expanded rung does for a held write", () => {
    const steps = [step({ name: "close_reading", facts: [fact({ status: "held" })] })];
    expect(turnWriteSummary(steps)).toBe("not recorded 1 fact");
  });

  it("counts across every write step of the turn, and pluralises", () => {
    const steps = [
      step({ name: "close_reading", facts: [fact(), fact()] }),
      step({ name: "assert_fact", facts: [fact({ status: "held" })] }),
    ];
    expect(turnWriteSummary(steps)).toBe("recorded 2 facts · not recorded 1 fact");
  });

  it("says nothing for a turn that only read, so a search keeps its step count", () => {
    expect(turnWriteSummary([step({ name: "search" })])).toBeUndefined();
  });

  it("ignores a failed write — it wrote nothing whatever its arguments claimed", () => {
    const steps = [step({ name: "close_reading", ok: false, facts: [fact()] })];
    expect(turnWriteSummary(steps)).toBeUndefined();
  });
});
