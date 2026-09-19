import { describe, expect, it } from "vitest";
import {
  domainWord,
  entityPhrase,
  ledgerRows,
  ledgerWord,
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
    result: undefined,
    view: undefined,
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
    expect(tallyWrites(s.facts)).toEqual({
      written: 2,
      replaced: 1,
      held: 1,
      already: 0,
      fromPhoto: 0,
    });
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
    // The two unchanged facts are the TAIL, never the news — without them "updated 1
    // fact" reads as though the other two had gone somewhere.
    expect(turnWriteSummary(steps)).toBe("updated 1 fact · 2 facts unchanged");
  });

  it("says a re-reading that changed nothing changed nothing, out loud", () => {
    // ⟲ This asserted `undefined`, on the reasoning that the step count was then the
    // honest headline. It is not an answer: the owner opened a re-read note and found a
    // turn headlined "1 step", which says neither that the pass ran nor that it agreed
    // with what was on file. "Nothing new" is a result, and he can accept it.
    const steps = [step({ name: "close_reading", facts: [fact({ outcome: "already" })] })];
    expect(turnWriteSummary(steps)).toBe("nothing new · 1 fact re-confirmed");
  });

  it("still says nothing for a turn that wrote no facts at all", () => {
    // The undefined case that remains, and the one the step count IS honest for: a read
    // turn. Nothing about the graph happened, so there is nothing to summarise.
    const steps = [step({ name: "search", facts: [] })];
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

  it("counts a minted entity ONCE, however many facts go on to name it", () => {
    // The shape the backend really emits, which the first version of the test above did
    // not: `close_reading` returns an `EntityRef` per touched handle PER FACT, and
    // `created` belongs to the HANDLE, so it stays true for the whole pass. Two facts
    // about Me and Boss ship four more refs, all `created`.
    //
    // Uncaught, that filled the card with "Me added / Boss added / Me added / Boss added"
    // and pushed the fact — the actual receipt — behind "+1 more", on the FIRST pass,
    // which is the one he reported. (A carried-over handle has `created: false`, so a
    // re-read never showed it.)
    const me = {
      kind: "entity" as const,
      entity_id: "e1",
      label: "Me",
      domain: "general" as const,
      created: true,
    };
    const boss = {
      kind: "entity" as const,
      entity_id: "e2",
      label: "Boss",
      domain: "general" as const,
      created: true,
    };
    const steps = [
      step({ name: "resolve_entity", entities: [me, boss] }),
      step({
        name: "close_reading",
        entities: [me, boss, me, boss],
        facts: [
          fact({ fact_id: "a", label: "Boss is Jeff's dog." }),
          fact({ fact_id: "b", label: "Boss is a labrador." }),
        ],
      }),
    ];
    expect(ledgerRows(steps)).toEqual([
      { key: "e:e1", text: "Me", face: "new", domain: "general" },
      { key: "e:e2", text: "Boss", face: "new", domain: "general" },
      { key: "f:a", text: "Boss is Jeff's dog.", face: "written", domain: "general" },
      { key: "f:b", text: "Boss is a labrador.", face: "written", domain: "general" },
    ]);
    // And every row addresses a distinct thing, so nothing renders on a duplicate key.
    const keys = ledgerRows(steps).map((r) => r.key);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("says nothing for a turn that only read, so a search keeps its step count", () => {
    expect(turnWriteSummary([step({ name: "search" })])).toBeUndefined();
  });

  it("ignores a failed write — it wrote nothing whatever its arguments claimed", () => {
    const steps = [step({ name: "close_reading", ok: false, facts: [fact()] })];
    expect(turnWriteSummary(steps)).toBeUndefined();
  });
});

describe("the ledger on the face of the turn", () => {
  it("lists what CHANGED, and leaves what was already on file to the summary", () => {
    // The owner's report: "I don't see how it actually added the entity to the database,
    // the conversation kinda looks like after that actually took place?" The agent's
    // prose is a claim; these lines are the receipt, and they render without a tap.
    const steps = [
      step({
        name: "resolve_entity",
        entities: [
          { kind: "entity", entity_id: "e1", label: "Boss", domain: "general", created: true },
          { kind: "entity", entity_id: "e2", label: "Me", domain: "general", created: false },
        ],
      }),
      step({
        name: "close_reading",
        facts: [
          fact({ fact_id: "a", label: "Boss is Jeff's dog." }),
          fact({ fact_id: "b", outcome: "already", label: "Jeff lives in the Marina." }),
        ],
      }),
    ];
    expect(ledgerRows(steps)).toEqual([
      { key: "e:e1", text: "Boss", face: "new", domain: "general" },
      { key: "f:a", text: "Boss is Jeff's dog.", face: "written", domain: "general" },
    ]);
  });

  it("says nothing for a re-reading that changed nothing", () => {
    // A ledger of twelve unchanged facts buries the one line that is news under eleven
    // that are not. The count of them is `turnWriteSummary`'s job, not this card's.
    const steps = [
      step({
        name: "close_reading",
        facts: [fact({ outcome: "already" }), fact({ outcome: "already" })],
      }),
    ];
    expect(ledgerRows(steps)).toEqual([]);
  });

  it("holds its line until the call that would make it has settled", () => {
    // In flight and failed alike: neither has a settled answer to what changed, and a
    // line that appears and then retracts is worse than one that arrives a second late.
    const live = step({ name: "close_reading", ok: undefined, facts: [fact()] });
    const dead = step({ name: "close_reading", ok: false, facts: [fact()] });
    expect(ledgerRows([live, dead])).toEqual([]);
  });

  it("speaks the same words the expanded rung does", () => {
    expect(ledgerWord("written")).toBe("recorded");
    expect(ledgerWord("replaced")).toBe("updated");
    expect(ledgerWord("held")).toBe("not recorded");
    // The entity case says what happened, rather than naming a write state it has none of.
    expect(ledgerWord("new")).toBe("added");
  });
});

describe("what a resolve did to the owner's cast", () => {
  it("names the records it made and the ones it matched", () => {
    // `stepWriteState` calls a mint-only resolve `none` on purpose — it wrote no fact.
    // The cost was a row with no mark at all, so the one call that creates his records
    // read as a call that did nothing.
    const s = step({
      name: "resolve_entity",
      entities: [
        { kind: "entity", entity_id: "e1", label: "Boss", domain: "general", created: true },
        { kind: "entity", entity_id: "e2", label: "Me", domain: "general", created: false },
      ],
    });
    expect(entityPhrase(s)).toBe("1 new · 1 already known");
  });

  it("says nothing when the refs cannot answer 'new or already mine?'", () => {
    // A ref persisted before `created` existed. A bare count of entities is the step
    // count again, in a costlier form.
    const s = step({
      name: "resolve_entity",
      entities: [{ kind: "entity", entity_id: "e1", label: "Boss", domain: "general" }],
    });
    expect(entityPhrase(s)).toBeUndefined();
    expect(entityPhrase(step({ name: "search" }))).toBeUndefined();
  });
});
