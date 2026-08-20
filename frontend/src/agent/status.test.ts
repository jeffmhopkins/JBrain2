import { describe, expect, it } from "vitest";
import { agentStatus, modelLoadStatus, planWaitingStatus } from "./status";
import type { TranscriptMessage } from "./transcript";

function asst(over: Partial<TranscriptMessage> = {}): TranscriptMessage {
  return {
    role: "assistant",
    text: "",
    tools: [],
    views: [],
    streaming: true,
    reasoning: "",
    thinking: false,
    ...over,
  };
}
const USER: TranscriptMessage = {
  role: "user",
  text: "hi",
  tools: [],
  views: [],
  streaming: false,
  reasoning: "",
  thinking: false,
};

describe("planWaitingStatus", () => {
  it("carries the countdown on emphasis as a between-steps waiting status", () => {
    expect(planWaitingStatus("0:54")).toEqual({
      kind: "waiting",
      label: "Starting next step in",
      emphasis: "0:54",
    });
  });
});

describe("modelLoadStatus", () => {
  it("names the model, its fraction, and the load's own start", () => {
    expect(modelLoadStatus("gpt-oss-120b", 0.43, 1000)).toEqual({
      kind: "loading",
      label: "Loading",
      emphasis: "gpt-oss-120b",
      percent: 0.43,
      sinceMs: 1000,
    });
  });

  it("names the prompt, not the model, once the model is already on the box", () => {
    // A prefill is a different wait with a different cause. "Loading gpt-oss-120b" answers
    // "why is nothing happening" while the weights are still arriving; once they have
    // arrived the model's name explains nothing and the prompt is the thing being eaten.
    expect(modelLoadStatus("gpt-oss-120b", 0.6, 1000, "prefill")).toEqual({
      kind: "loading",
      label: "Reading",
      emphasis: "your prompt",
      percent: 0.6,
      sinceMs: 1000,
    });
  });

  it("carries a null fraction through rather than inventing a zero", () => {
    // A gateway build that prints no parseable progress is the ordinary case; "0%" on a
    // load that is halfway through reads as stuck.
    expect(modelLoadStatus("qwen35", null, 1000).percent).toBeNull();
  });
});

describe("agentStatus", () => {
  it("is null when idle (no turn, or the last message is the user's)", () => {
    expect(agentStatus([])).toBeNull();
    expect(agentStatus([USER])).toBeNull();
  });

  it("reads as thinking before any text or tool arrives", () => {
    expect(agentStatus([USER, asst()])).toEqual({
      kind: "thinking",
      label: "Thinking it through",
      turnKey: "#1",
    });
  });

  it("names the in-flight tool with a friendly verb + object", () => {
    const s = agentStatus([USER, asst({ tools: [{ id: "c1", name: "search" }] })]);
    expect(s).toEqual({ kind: "tool", label: "Searching", emphasis: "your notes", turnKey: "#1" });
  });

  it("shows a multi-phase tool's live phase label instead of the generic verb", () => {
    const s = agentStatus([
      USER,
      asst({
        tools: [
          {
            id: "c1",
            name: "analyze_video",
            progress: { step: 12, total: 30, label: "Analyzing frame 12/30" },
          },
        ],
      }),
    ]);
    expect(s).toEqual({ kind: "tool", label: "Analyzing frame 12/30", turnKey: "#1" });
  });

  it("maps a connector lookup_* tool to its subject", () => {
    const s = agentStatus([USER, asst({ tools: [{ id: "c1", name: "lookup_medication" }] })]);
    expect(s).toEqual({ kind: "tool", label: "Checking", emphasis: "medication", turnKey: "#1" });
  });

  it("falls back to a generic for an unmapped tool", () => {
    const s = agentStatus([USER, asst({ tools: [{ id: "c1", name: "frobnicate" }] })]);
    expect(s).toEqual({ kind: "tool", label: "Using", emphasis: "frobnicate", turnKey: "#1" });
  });

  it("reads as answering once text streams and no tool is in flight", () => {
    const m = asst({ text: "Here", tools: [{ id: "c1", name: "search", ok: true }] });
    expect(agentStatus([USER, m])).toEqual({
      kind: "answering",
      label: "Writing the answer",
      turnKey: "#1",
    });
  });

  it("confirms a clean finish with the tool count", () => {
    const m = asst({
      streaming: false,
      stopReason: "end_turn",
      text: "done",
      tools: [
        { id: "c1", name: "search", ok: true },
        { id: "c2", name: "read_note", ok: true },
      ],
    });
    expect(agentStatus([USER, m])).toEqual({
      kind: "done",
      label: "Answered · 2 tools used",
      turnKey: "#1",
    });
  });

  it("says just 'Answered' when no tools were used", () => {
    const m = asst({ streaming: false, stopReason: "end_turn", text: "hi" });
    expect(agentStatus([USER, m])).toEqual({ kind: "done", label: "Answered", turnKey: "#1" });
  });

  it("surfaces guardrail/error stops", () => {
    const stop = (r: string) => agentStatus([USER, asst({ streaming: false, stopReason: r })]);
    expect(stop("budget")).toEqual({
      kind: "error",
      label: "Stopped — hit the budget",
      turnKey: "#1",
    });
    expect(stop("too_many_errors")).toEqual({
      kind: "error",
      label: "Stopped — tools kept failing",
      turnKey: "#1",
    });
    expect(stop("error")).toEqual({ kind: "error", label: "Something went wrong", turnKey: "#1" });
    expect(stop("context_overflow")).toEqual({
      kind: "error",
      label: "This model ran out of context — raise its window in Settings or start a new chat",
      turnKey: "#1",
    });
  });

  it("reads a user-initiated Stop as calm (a done register), not an error", () => {
    const s = agentStatus([USER, asst({ streaming: false, stopReason: "stopped" })]);
    expect(s).toEqual({ kind: "done", label: "Stopped", turnKey: "#1" });
  });

  it("tags each phase with a turn key that bumps as new turns are appended", () => {
    const done = asst({ streaming: false, stopReason: "end_turn", text: "a" });
    // Steady across a turn's phases; a fresh turn (user + assistant appended) bumps it.
    expect(agentStatus([USER, asst()])?.turnKey).toBe("#1");
    expect(agentStatus([USER, done, USER, asst()])?.turnKey).toBe("#3");
  });

  it("scopes the turn key by session so two conversations' same-index turns differ", () => {
    // The bug: the message-index alone collides — every conversation's first turn is
    // index 1 — so the status line (which persists across chat switches) failed to
    // re-anchor its timer and inherited the prior conversation's start time.
    const a = agentStatus([USER, asst()], "sess-A")?.turnKey;
    const b = agentStatus([USER, asst()], "sess-B")?.turnKey;
    expect(a).toBe("sess-A#1");
    expect(b).toBe("sess-B#1");
    expect(a).not.toBe(b);
  });
});
