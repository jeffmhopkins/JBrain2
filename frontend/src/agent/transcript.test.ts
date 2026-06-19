import { describe, expect, it } from "vitest";
import { type TranscriptMessage, applyEvent } from "./transcript";

function streaming(): TranscriptMessage {
  return {
    role: "assistant",
    text: "",
    tools: [],
    views: [],
    streaming: true,
    reasoning: "",
    thinking: false,
  };
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

  it("accumulates reasoning and tracks the live thinking phase", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "reasoning_delta", text: "let me " });
    ms = applyEvent(ms, { type: "reasoning_delta", text: "think" });
    // While only reasoning has arrived (no answer yet), the bubble is "thinking".
    expect(ms[0]?.reasoning).toBe("let me think");
    expect(ms[0]?.thinking).toBe(true);
    // The first answer token ends the thinking phase (collapse the disclosure).
    ms = applyEvent(ms, { type: "text_delta", text: "the answer" });
    expect(ms[0]?.thinking).toBe(false);
    expect(ms[0]?.reasoning).toBe("let me think");
    // Reasoning is never folded into the answer text.
    expect(ms[0]?.text).toBe("the answer");
  });

  it("stops thinking when a reasoning-only turn settles", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "reasoning_delta", text: "hmm" });
    expect(ms[0]?.thinking).toBe(true);
    ms = applyEvent(ms, { type: "done", stop_reason: "end_turn" });
    expect(ms[0]?.thinking).toBe(false);
  });

  it("keeps a tool call's non-empty arguments, but omits an empty object", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, {
      type: "tool_call",
      id: "c1",
      name: "search",
      arguments: { query: "born", limit: 8 },
    });
    ms = applyEvent(ms, { type: "tool_call", id: "c2", name: "recall", arguments: {} });
    expect(ms[0]?.tools[0]?.args).toEqual({ query: "born", limit: 8 });
    expect(ms[0]?.tools[1]).not.toHaveProperty("args");
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
      {
        role: "user",
        text: "hi",
        tools: [],
        views: [],
        streaming: false,
        reasoning: "",
        thinking: false,
      },
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

  it("attaches resolved entities from a tool result to its tool", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "find_entity", arguments: {} });
    ms = applyEvent(ms, {
      type: "tool_result",
      tool_call_id: "c1",
      ok: true,
      summary: "1",
      entities: [{ kind: "entity", entity_id: "e1", label: "Celine", domain: "general" }],
    });
    expect(ms[0]?.tools[0]?.entities).toEqual([
      { kind: "entity", entity_id: "e1", label: "Celine", domain: "general" },
    ]);
  });

  it("attaches a reflexion verdict (ungrounded claims) to the settled turn", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "text_delta", text: "The roof needs replacing." });
    ms = applyEvent(ms, { type: "done", stop_reason: "end_turn" });
    ms = applyEvent(ms, {
      type: "verdict",
      passed: false,
      score: 0.5,
      issues: ["claim not grounded in retrieved sources: The roof needs replacing."],
      ungrounded_claims: ["The roof needs replacing."],
    });
    expect(ms[0]?.verdict).toEqual({
      passed: false,
      score: 0.5,
      issues: ["claim not grounded in retrieved sources: The roof needs replacing."],
      ungroundedClaims: ["The roof needs replacing."],
    });
  });

  it("leaves a turn unflagged when no verdict arrives", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "text_delta", text: "All grounded." });
    ms = applyEvent(ms, { type: "done", stop_reason: "end_turn" });
    expect(ms[0]?.verdict).toBeUndefined();
  });

  it("attaches a passing verdict without claims (nothing to flag)", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "done", stop_reason: "end_turn" });
    ms = applyEvent(ms, { type: "verdict", passed: true, score: 1 });
    expect(ms[0]?.verdict).toEqual({ passed: true, score: 1, issues: [], ungroundedClaims: [] });
  });

  it("marks a turn answered from general knowledge (no retrieval)", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "text_delta", text: "Jeff is a short form of Jeffrey." });
    ms = applyEvent(ms, { type: "done", stop_reason: "end_turn" });
    ms = applyEvent(ms, { type: "general_knowledge" });
    expect(ms[0]?.generalKnowledge).toBe(true);
    // Neutral, not a verdict — the two never co-occur.
    expect(ms[0]?.verdict).toBeUndefined();
  });

  it("leaves a grounded turn unmarked (no general_knowledge event)", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "text_delta", text: "Your name is Jeff." });
    ms = applyEvent(ms, { type: "done", stop_reason: "end_turn" });
    expect(ms[0]?.generalKnowledge).toBeUndefined();
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
