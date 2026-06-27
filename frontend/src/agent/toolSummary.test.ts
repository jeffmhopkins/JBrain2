import { describe, expect, it } from "vitest";
import { toolStep } from "./toolSummary";
import type { ToolActivity } from "./transcript";
import type { EntityRef } from "./types";

function tool(over: Partial<ToolActivity> & { name: string }): ToolActivity {
  return { id: "c1", ok: true, ...over };
}

describe("toolStep", () => {
  it("prefers structured sources over parsing the summary, stripping marks", () => {
    const step = toolStep(
      tool({
        name: "search",
        summary: "- note zzz [general] 2026-06-12: ignored",
        sources: [{ noteId: "n1", domain: "health", text: "I was <mark>born</mark> in 1986" }],
      }),
    );
    expect(step.sources).toEqual([{ noteId: "n1", domain: "health", text: "I was born in 1986" }]);
  });

  it("parses search results into source refs (id, domain, mark-stripped text)", () => {
    const summary =
      "- note abc-123 [general] 2026-06-12: I was <mark>born</mark> March 19, 1986\n" +
      "- note def-456 [health] 2026-01-02: albumin trending up";
    const step = toolStep(tool({ name: "search", summary }));
    expect(step.label).toBe("Searched your notes");
    expect(step.sources).toEqual([
      { noteId: "abc-123", domain: "general", text: "I was born March 19, 1986" },
      { noteId: "def-456", domain: "health", text: "albumin trending up" },
    ]);
  });

  it("ignores non-result lines in a search summary (e.g. the degraded header)", () => {
    const summary =
      "(keyword-only search — semantic ranking unavailable)\n- note z9 [general] 2026-06-12: hi";
    expect(toolStep(tool({ name: "search", summary })).sources).toEqual([
      { noteId: "z9", domain: "general", text: "hi" },
    ]);
  });

  it("parses a read_note into a single source from its head + body", () => {
    const summary = "note abc-123 [general] 2026-06-12\nI was born March 19, 1986";
    const step = toolStep(tool({ name: "read_note", summary }));
    expect(step.label).toBe("Read a note");
    expect(step.sources).toEqual([
      { noteId: "abc-123", domain: "general", text: "I was born March 19, 1986" },
    ]);
  });

  it("gives other tools a friendly label and no sources", () => {
    expect(toolStep(tool({ name: "recall", summary: "stuff" })).sources).toEqual([]);
    expect(toolStep(tool({ name: "recall" })).label).toBe("Recalled past notes");
    expect(toolStep(tool({ name: "propose_correction" })).label).toBe("Staged a proposal");
    expect(toolStep(tool({ name: "lookup_medication" })).label).toBe("Checked medication");
    expect(toolStep(tool({ name: "queued" })).label).toBe("Queued a job");
  });

  it("falls back to the raw name for an unmapped tool", () => {
    expect(toolStep(tool({ name: "frobnicate" })).label).toBe("frobnicate");
  });

  it("labels the entity-graph tools and carries their resolved entities through", () => {
    expect(toolStep(tool({ name: "relate" })).label).toBe("Followed a relationship");
    expect(toolStep(tool({ name: "find_entity" })).label).toBe("Found an entity");
    const ents: EntityRef[] = [
      { kind: "entity", entity_id: "e9", label: "Celine", domain: "general" },
    ];
    const step = toolStep(tool({ name: "relate", entities: ents }));
    expect(step.entities).toEqual(ents);
    // A tool that resolved nothing gets an empty list, never undefined.
    expect(toolStep(tool({ name: "search" })).entities).toEqual([]);
  });

  it("carries the call's arguments and verbatim summary through for the detail rungs", () => {
    const step = toolStep(
      tool({ name: "search", args: { query: "born", limit: 8 }, summary: "raw text" }),
    );
    expect(step.args).toEqual({ query: "born", limit: 8 });
    expect(step.summary).toBe("raw text");
    // A tool with no arguments leaves args undefined (nothing to render).
    expect(toolStep(tool({ name: "recall" })).args).toBeUndefined();
  });

  it("carries the in-flight ok state through", () => {
    const t: ToolActivity = { id: "c1", name: "search" };
    expect(toolStep(t).ok).toBeUndefined();
    expect(toolStep(tool({ name: "search", ok: false })).ok).toBe(false);
  });

  it("labels the web tools and carries their web sources through", () => {
    expect(toolStep(tool({ name: "web_search" })).label).toBe("Searched the web");
    expect(toolStep(tool({ name: "web_fetch" })).label).toBe("Read a web page");
    const webSources = [{ url: "https://x.example/a", title: "A page" }];
    const step = toolStep(tool({ name: "web_search", webSources }));
    expect(step.webSources).toEqual(webSources);
    // A tool that surfaced none gets an empty list, never undefined.
    expect(toolStep(tool({ name: "search" })).webSources).toEqual([]);
  });
});
