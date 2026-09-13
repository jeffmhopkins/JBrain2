import { describe, expect, it } from "vitest";
import { domainWord, stepWriteState, tallyWrites, writeDomains, writePhrase } from "./entityWrites";
import type { ToolStep } from "./toolSummary";
import type { FactWrite } from "./types";

function fact(over: Partial<FactWrite> = {}): FactWrite {
  return {
    fact_id: over.fact_id ?? `f${Math.random()}`,
    label: "Me lives_in Marina District",
    domain: "general",
    status: "written",
    ...over,
  };
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
    expect(writePhrase(s)).toBe("nothing written");
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
    expect(writePhrase(s)).toBe("2 written · 1 replaced · 1 held · general");
    expect(tallyWrites(s.facts)).toEqual({ written: 2, replaced: 1, held: 1, fromPhoto: 0 });
  });

  it("marks an attachment-sourced write as from a photo (D12)", () => {
    const s = step({
      name: "assert_fact",
      facts: [fact({ from_attachment: true, domain: "health" })],
    });
    expect(writePhrase(s)).toBe("1 written · health · from a photo");
  });

  it("says truncated, so a partial batch is never shown as a whole one", () => {
    const s = step({ name: "assert_fact", facts: [fact()], truncated: true });
    expect(writePhrase(s)).toBe("1 written · general · truncated");
    // And on the two states that carry no write list either.
    expect(writePhrase(step({ name: "assert_fact", ok: false, truncated: true }))).toBe(
      "failed · truncated",
    );
    expect(writePhrase(step({ name: "assert_fact", truncated: true }))).toBe(
      "nothing written · truncated",
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
