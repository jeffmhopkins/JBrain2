import { describe, expect, it } from "vitest";
import type { TranscriptTurn } from "./types";
import { fromTurn } from "./useFullBrain";

// The transcript hydrator has to survive a server-authored tool step that omits `sources`.
// A background deepest-research progress tick persists exactly such a step (deepest_progress.
// _deepest_view_step), and `t.tools.map` is eager — so one un-guarded `tool.sources.map`
// would throw and blank the ENTIRE session on reopen, not just that step. These drive the
// real fromTurn over the exact persisted shape; the deserialization path had no test before.

// A persisted deepest-run progress turn as the backend actually stores it: a raw dict with
// no `sources` key. The TS type says `sources` is required, but the wire is `list[dict]` —
// so we cast to model the real (looser) runtime payload.
function deepestProgressTurn(withSources: boolean): TranscriptTurn {
  const tool = {
    name: "deepest_research",
    args: {},
    ok: true,
    summary: "",
    ...(withSources ? { sources: [] } : {}),
    view: {
      view: "deepest_run",
      surface: "inline",
      data: { round: 2, sources: 5, coverage: "in progress", status: "running", step: 5 },
      refs: [],
    },
  };
  return {
    role: "assistant",
    content: "Deepest research · round 2 · 5 finding(s) so far · in progress · still going",
    tools: [tool as unknown as TranscriptTurn["tools"][number]],
  };
}

describe("fromTurn", () => {
  it("does not throw on a server-authored step that omits sources (deepest tick)", () => {
    // Before the `?? []` guard this threw `Cannot read properties of undefined (reading 'map')`.
    expect(() => fromTurn(deepestProgressTurn(false))).not.toThrow();
    const msg = fromTurn(deepestProgressTurn(false));
    expect(msg.tools[0]?.sources).toEqual([]);
    // The deepest_run view still round-trips so the card renders.
    expect(msg.views[0]?.view).toBe("deepest_run");
  });

  it("preserves an explicit empty sources array (canonical shape)", () => {
    const msg = fromTurn(deepestProgressTurn(true));
    expect(msg.tools[0]?.sources).toEqual([]);
    expect(msg.views[0]?.view).toBe("deepest_run");
  });

  it("maps real note sources through", () => {
    const turn: TranscriptTurn = {
      role: "assistant",
      content: "answer",
      tools: [
        {
          id: "t1",
          name: "search",
          ok: true,
          sources: [{ note_id: "n1", domain: "general", snippet: "hit" }],
        },
      ],
    };
    expect(fromTurn(turn).tools[0]?.sources).toEqual([
      { noteId: "n1", domain: "general", text: "hit" },
    ]);
  });
});
