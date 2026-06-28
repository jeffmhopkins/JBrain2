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
    // textOffset 12 = the prose length ("let me check") when the call was made.
    expect(turn?.tools).toEqual([
      { id: "c1", name: "search", ok: true, summary: "found 3", sources: [], textOffset: 12 },
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

  it("attaches web sources (favicon citation chips) from a tool result to its tool", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "web_search", arguments: {} });
    ms = applyEvent(ms, {
      type: "tool_result",
      tool_call_id: "c1",
      ok: true,
      summary: "Web results:\n- A page\n  https://x.example/a",
      web_sources: [{ url: "https://x.example/a", title: "A page" }],
    });
    expect(ms[0]?.tools[0]?.webSources).toEqual([{ url: "https://x.example/a", title: "A page" }]);
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

  it("tracks an image tool's live progress, then clears it when the result lands", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "g1", name: "generate_image", arguments: {} });
    ms = applyEvent(ms, { type: "tool_progress", tool_call_id: "g1", step: 5, total: 20 });
    // The first tick (no preview yet) records step/total only.
    expect(ms[0]?.tools[0]?.progress).toEqual({ step: 5, total: 20 });
    ms = applyEvent(ms, {
      type: "tool_progress",
      tool_call_id: "g1",
      step: 20,
      total: 20,
      preview: "data:image/jpeg;base64,AAA",
    });
    expect(ms[0]?.tools[0]?.progress).toEqual({
      step: 20,
      total: 20,
      preview: "data:image/jpeg;base64,AAA",
    });
    // The result settles the tool: the live progress drops, but the last preview frame
    // is carried as `preview` so the final image view can hold it until the full-res
    // image loads (no blank gap).
    ms = applyEvent(ms, { type: "tool_result", tool_call_id: "g1", ok: true, summary: "done" });
    expect(ms[0]?.tools[0]?.progress).toBeUndefined();
    expect(ms[0]?.tools[0]?.preview).toBe("data:image/jpeg;base64,AAA");
    expect(ms[0]?.tools[0]?.ok).toBe(true);
  });

  it("carries a multi-phase tool's text label into progress", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "v1", name: "analyze_video", arguments: {} });
    ms = applyEvent(ms, {
      type: "tool_progress",
      tool_call_id: "v1",
      step: 12,
      total: 30,
      label: "Analyzing frame 12/30",
    });
    expect(ms[0]?.tools[0]?.progress).toEqual({
      step: 12,
      total: 30,
      label: "Analyzing frame 12/30",
    });
  });

  it("ignores progress for an unknown tool call", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_progress", tool_call_id: "ghost", step: 1, total: 4 });
    expect(ms[0]?.tools).toEqual([]);
  });

  it("folds a sub-agent fan onto its spawn tool call (spawned → progress → done)", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "spawn_subagent", arguments: {} });
    ms = applyEvent(ms, {
      type: "subagent_spawned",
      tool_call_id: "c1",
      child_id: "k1",
      persona: "research",
      label: "Pricing",
      depth: 1,
    });
    ms = applyEvent(ms, {
      type: "subagent_progress",
      tool_call_id: "c1",
      child_id: "k1",
      phase: "researching",
      tree_spent: 300,
      tree_budget: 1200,
    });
    ms = applyEvent(ms, {
      type: "subagent_done",
      tool_call_id: "c1",
      child_id: "k1",
      ok: true,
      stop_reason: "end_turn",
      summary: "3 tiers",
      tree_spent: 500,
      tree_budget: 1200,
    });
    const fan = ms[0]?.tools[0]?.fan;
    expect(fan?.treeSpent).toBe(500);
    expect(fan?.treeBudget).toBe(1200);
    expect(fan?.children).toEqual([
      {
        childId: "k1",
        persona: "research",
        label: "Pricing",
        depth: 1,
        phase: "end_turn",
        status: "done",
        stopReason: "end_turn",
        summary: "3 tiers",
      },
    ]);
  });

  it("marks a failed child rose and is idempotent on a replayed spawn", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "spawn_subagent", arguments: {} });
    const spawned = {
      type: "subagent_spawned" as const,
      tool_call_id: "c1",
      child_id: "k1",
      persona: "review",
      label: "Cross-check",
      depth: 1,
    };
    ms = applyEvent(ms, spawned);
    ms = applyEvent(ms, spawned); // reconnect replay — must not duplicate
    ms = applyEvent(ms, {
      type: "subagent_done",
      tool_call_id: "c1",
      child_id: "k1",
      ok: false,
      stop_reason: "error",
      summary: "ERROR: web_fetch timed out",
      tree_spent: 200,
      tree_budget: 1200,
    });
    const fan = ms[0]?.tools[0]?.fan;
    expect(fan?.children).toHaveLength(1);
    expect(fan?.children[0]?.status).toBe("failed");
  });

  it("settles a still-running child to cancelled when the turn ends (Stop/cancel)", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "spawn_subagent", arguments: {} });
    ms = applyEvent(ms, {
      type: "subagent_spawned",
      tool_call_id: "c1",
      child_id: "k1",
      persona: "research",
      label: "Pricing",
      depth: 1,
    });
    // The turn ends (a cancel cascades no per-child done) — the row must not stay "running".
    ms = applyEvent(ms, { type: "done", stop_reason: "stopped" });
    const c = ms[0]?.tools[0]?.fan?.children[0];
    expect(c?.status).toBe("failed");
    expect(c?.stopReason).toBe("cancelled");
  });

  it("ignores a subagent event for an unknown spawn call", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, {
      type: "subagent_spawned",
      tool_call_id: "ghost",
      child_id: "k1",
      persona: "research",
      label: "x",
      depth: 1,
    });
    expect(ms[0]?.tools).toEqual([]);
  });
});
