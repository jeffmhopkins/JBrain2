import { describe, expect, it } from "vitest";
import { type TranscriptMessage, applyEvent } from "./transcript";

function streaming(): TranscriptMessage {
  return { role: "assistant", text: "", tools: [], views: [], streaming: true };
}

describe("applyEvent reducer", () => {
  it("accumulates text, pairs a tool result to its call, and closes on done", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "text_delta", text: "let me " });
    ms = applyEvent(ms, { type: "text_delta", text: "check" });
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "search", arguments: {} });
    ms = applyEvent(ms, { type: "tool_result", tool_call_id: "c1", ok: true, summary: "found 3" });
    ms = applyEvent(ms, { type: "done", stop_reason: "end_turn" });
    const turn = ms[0];
    expect(turn?.text).toBe("let me check");
    expect(turn?.tools).toEqual([
      { id: "c1", name: "search", ok: true, summary: "found 3", sources: [] },
    ]);
    expect(turn?.streaming).toBe(false);
    expect(turn?.stopReason).toBe("end_turn");
  });

  it("collects tool_view payloads in order", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, {
      type: "tool_view",
      tool_call_id: "c1",
      view: { view: "stat_block", surface: "inline", data: { value: "1" }, refs: [] },
    });
    expect(ms[0]?.views).toHaveLength(1);
  });

  it("ignores events when there is no live assistant turn", () => {
    const ms: TranscriptMessage[] = [
      { role: "user", text: "hi", tools: [], views: [], streaming: false },
    ];
    expect(applyEvent(ms, { type: "text_delta", text: "x" })).toBe(ms);
  });

  it("attaches a staged proposal from a tool result to its tool", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "propose_correction", arguments: {} });
    ms = applyEvent(ms, {
      type: "tool_result",
      tool_call_id: "c1",
      ok: true,
      summary: "staged",
      proposal: { proposal_id: "p1", kind: "correction" },
    });
    expect(ms[0]?.tools[0]?.proposal).toEqual({ proposal_id: "p1", kind: "correction" });
  });

  it("attaches structured sources from a tool result to its tool", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "search", arguments: {} });
    ms = applyEvent(ms, {
      type: "tool_result",
      tool_call_id: "c1",
      ok: true,
      summary: "2 notes",
      sources: [
        { note_id: "n1", domain: "general", snippet: "born March" },
        { note_id: "n2", domain: "health", snippet: "albumin" },
      ],
    });
    expect(ms[0]?.tools[0]?.sources).toEqual([
      { noteId: "n1", domain: "general", text: "born March" },
      { noteId: "n2", domain: "health", text: "albumin" },
    ]);
  });
});
