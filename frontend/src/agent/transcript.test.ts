import { describe, expect, it } from "vitest";
import { type TranscriptMessage, applyEvent } from "./transcript";
import type { ChatEvent } from "./types";

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
    // textOffset 12 = the prose length ("let me check") when the call was made;
    // reasoningOffset 0 = no reasoning had streamed before it.
    expect(turn?.tools).toEqual([
      {
        id: "c1",
        name: "search",
        ok: true,
        summary: "found 3",
        sources: [],
        textOffset: 12,
        reasoningOffset: 0,
      },
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

  it("records each tool's reasoning offset so it interleaves into the thinking trace", () => {
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "reasoning_delta", text: "first" });
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "search", arguments: {} });
    ms = applyEvent(ms, { type: "reasoning_delta", text: " then more" });
    ms = applyEvent(ms, { type: "tool_call", id: "c2", name: "read_note", arguments: {} });
    // Each call's offset is the reasoning length at the moment it ran — the split point
    // the "Thinking" disclosure weaves the tool into.
    expect(ms[0]?.tools[0]?.reasoningOffset).toBe("first".length);
    expect(ms[0]?.tools[1]?.reasoningOffset).toBe("first then more".length);
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

  it("supersedes the subagent_synthesis roster instead of stacking each update", () => {
    let ms: TranscriptMessage[] = [streaming()];
    const syn = (ran: number, tool_call_id = "sp1"): ChatEvent => ({
      type: "tool_view",
      tool_call_id,
      view: {
        view: "subagent_synthesis",
        surface: "inline",
        data: { ran, failed: 0, truncated: false, children: [] },
        refs: [],
      },
    });
    // The fan re-emits the roster as each child settles (ran 1 → 2), then once more as
    // the final result — three tool_view events for one fan.
    ms = applyEvent(ms, syn(1));
    ms = applyEvent(ms, syn(2));
    ms = applyEvent(ms, syn(2));
    // Only the latest survives — ONE card, not three (the bug was "1 of 1 / 2 of 2 / 2 of 2").
    expect(ms[0]?.views).toHaveLength(1);
    expect(ms[0]?.views[0]?.data.ran).toBe(2);
    // A second, distinct fan keeps its own card (keyed by tool_call_id).
    ms = applyEvent(ms, syn(1, "sp2"));
    expect(ms[0]?.views).toHaveLength(2);
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
      step: 2,
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
        step: 2,
        stopReason: "end_turn",
        summary: "3 tiers",
      },
    ]);
  });

  it("folds a sub-agent's live context fill onto its child (the per-row meter)", () => {
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
      type: "subagent_usage",
      tool_call_id: "c1",
      child_id: "k1",
      used: 18_000,
      context_window: 131_072,
    });
    const child = ms[0]?.tools[0]?.fan?.children[0];
    expect(child?.usedTokens).toBe(18_000);
    expect(child?.contextWindow).toBe(131_072);
    // It updates context only — a usage tick must not disturb the child's phase/status.
    expect(child?.phase).toBe("queued");
  });

  it("accumulates a child's live answer and reasoning deltas onto its row", () => {
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
      type: "subagent_delta",
      tool_call_id: "c1",
      child_id: "k1",
      channel: "reasoning",
      text: "let me ",
    });
    ms = applyEvent(ms, {
      type: "subagent_delta",
      tool_call_id: "c1",
      child_id: "k1",
      channel: "reasoning",
      text: "search",
    });
    ms = applyEvent(ms, {
      type: "subagent_delta",
      tool_call_id: "c1",
      child_id: "k1",
      channel: "answer",
      text: "3 tiers",
    });
    const child = ms[0]?.tools[0]?.fan?.children[0];
    // Consecutive reasoning deltas coalesce into one trace run; the answer stays separate.
    expect(child?.liveTrace).toEqual([{ kind: "reasoning", text: "let me search" }]);
    expect(child?.liveText).toBe("3 tiers");
  });

  it("interleaves a child's tool calls into its trace at the point they occurred", () => {
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
    // A reasoning run, then a search, then more reasoning, then a fetch — the trace keeps
    // them in arrival order so a tool call renders where it actually happened.
    ms = applyEvent(ms, {
      type: "subagent_delta",
      tool_call_id: "c1",
      child_id: "k1",
      channel: "reasoning",
      text: "let me look",
    });
    ms = applyEvent(ms, {
      type: "subagent_tool",
      tool_call_id: "c1",
      child_id: "k1",
      name: "web_search",
      arg: "pricing tiers",
      ok: true,
    });
    ms = applyEvent(ms, {
      type: "subagent_delta",
      tool_call_id: "c1",
      child_id: "k1",
      channel: "reasoning",
      text: "now the source",
    });
    ms = applyEvent(ms, {
      type: "subagent_tool",
      tool_call_id: "c1",
      child_id: "k1",
      name: "web_fetch",
      arg: "https://x.test",
      ok: false,
    });
    expect(ms[0]?.tools[0]?.fan?.children[0]?.liveTrace).toEqual([
      { kind: "reasoning", text: "let me look" },
      { kind: "tool", name: "web_search", arg: "pricing tiers", ok: true },
      { kind: "reasoning", text: "now the source" },
      { kind: "tool", name: "web_fetch", arg: "https://x.test", ok: false },
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

  it("lazily materializes a child whose subagent_spawned never arrived (reconnect/evicted)", () => {
    // Fix #3: a subagent_progress whose subagent_spawned was missed (a reconnect that
    // resumed mid-fan, or a frame evicted from the live buffer) must NOT be dropped — it
    // creates a placeholder child under the spawn step instead of silently no-op'ing.
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "spawn_subagent", arguments: {} });
    ms = applyEvent(ms, {
      type: "subagent_progress",
      tool_call_id: "c1",
      child_id: "k1",
      phase: "reading",
      step: 4,
      tree_spent: 40,
      tree_budget: 100,
    });
    const child = ms[0]?.tools[0]?.fan?.children[0];
    // The placeholder carries the live progress; its persona/label are the stand-in until
    // a later subagent_spawned fills them in.
    expect(child?.childId).toBe("k1");
    expect(child?.status).toBe("running");
    expect(child?.phase).toBe("reading");
    expect(child?.step).toBe(4);
    expect(ms[0]?.tools[0]?.fan?.treeSpent).toBe(40);
  });

  it("materializes a child from a subagent_delta, then a late spawned upserts it", () => {
    // A delta arrives before the spawn frame (the evicted/reconnect case) — it creates the
    // placeholder and accumulates the live text; the LATE subagent_spawned then upserts the
    // real persona/label by child_id WITHOUT dropping the progress that already folded.
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "spawn_subagent", arguments: {} });
    ms = applyEvent(ms, {
      type: "subagent_delta",
      tool_call_id: "c1",
      child_id: "k1",
      channel: "answer",
      text: "partial child answer",
    });
    // Placeholder created, live text accumulated.
    expect(ms[0]?.tools[0]?.fan?.children).toHaveLength(1);
    expect(ms[0]?.tools[0]?.fan?.children[0]?.liveText).toBe("partial child answer");

    ms = applyEvent(ms, {
      type: "subagent_spawned",
      tool_call_id: "c1",
      child_id: "k1",
      persona: "review",
      label: "Cross-check",
      depth: 1,
    });
    const child = ms[0]?.tools[0]?.fan?.children[0];
    // The same single row, now with its real persona/label — and its earlier liveText kept.
    expect(ms[0]?.tools[0]?.fan?.children).toHaveLength(1);
    expect(child?.persona).toBe("review");
    expect(child?.label).toBe("Cross-check");
    expect(child?.liveText).toBe("partial child answer");
  });

  it("does not reset a progressed child back to queued on a late spawned upsert", () => {
    // The upsert only resets phase→"queued" for a never-progressed row. A child that
    // already has a `step` (it's working) must not be dragged back to queued when its
    // (replayed/late) subagent_spawned lands.
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "spawn_subagent", arguments: {} });
    ms = applyEvent(ms, {
      type: "subagent_progress",
      tool_call_id: "c1",
      child_id: "k1",
      phase: "researching",
      step: 2,
      tree_spent: 10,
      tree_budget: 100,
    });
    ms = applyEvent(ms, {
      type: "subagent_spawned",
      tool_call_id: "c1",
      child_id: "k1",
      persona: "research",
      label: "Pricing",
      depth: 1,
    });
    const child = ms[0]?.tools[0]?.fan?.children[0];
    expect(child?.phase).toBe("researching"); // not dragged back to "queued"
    expect(child?.step).toBe(2);
    expect(child?.label).toBe("Pricing"); // but its real label did fill in
  });

  it("upserts a normal spawned-first child without duplicating it (idempotent reconnect)", () => {
    // The common case still holds: a child whose subagent_spawned arrives first, then a
    // replayed spawned (a stale-offset reconnect), upserts by child_id — one row, queued.
    let ms: TranscriptMessage[] = [streaming()];
    ms = applyEvent(ms, { type: "tool_call", id: "c1", name: "spawn_subagent", arguments: {} });
    const spawned = {
      type: "subagent_spawned" as const,
      tool_call_id: "c1",
      child_id: "k1",
      persona: "research",
      label: "Pricing",
      depth: 1,
    };
    ms = applyEvent(ms, spawned);
    ms = applyEvent(ms, spawned); // reconnect replay
    expect(ms[0]?.tools[0]?.fan?.children).toHaveLength(1);
    expect(ms[0]?.tools[0]?.fan?.children[0]?.phase).toBe("queued");
  });
});
