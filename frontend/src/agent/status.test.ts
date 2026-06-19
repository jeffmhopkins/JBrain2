import { describe, expect, it } from "vitest";
import { agentStatus } from "./status";
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

describe("agentStatus", () => {
  it("is null when idle (no turn, or the last message is the user's)", () => {
    expect(agentStatus([])).toBeNull();
    expect(agentStatus([USER])).toBeNull();
  });

  it("reads as thinking before any text or tool arrives", () => {
    expect(agentStatus([USER, asst()])).toEqual({
      kind: "thinking",
      label: "Thinking it through",
    });
  });

  it("names the in-flight tool with a friendly verb + object", () => {
    const s = agentStatus([USER, asst({ tools: [{ id: "c1", name: "search" }] })]);
    expect(s).toEqual({ kind: "tool", label: "Searching", emphasis: "your notes" });
  });

  it("maps a connector lookup_* tool to its subject", () => {
    const s = agentStatus([USER, asst({ tools: [{ id: "c1", name: "lookup_medication" }] })]);
    expect(s).toEqual({ kind: "tool", label: "Checking", emphasis: "medication" });
  });

  it("falls back to a generic for an unmapped tool", () => {
    const s = agentStatus([USER, asst({ tools: [{ id: "c1", name: "frobnicate" }] })]);
    expect(s).toEqual({ kind: "tool", label: "Using", emphasis: "frobnicate" });
  });

  it("reads as answering once text streams and no tool is in flight", () => {
    const m = asst({ text: "Here", tools: [{ id: "c1", name: "search", ok: true }] });
    expect(agentStatus([USER, m])).toEqual({ kind: "answering", label: "Writing the answer" });
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
    expect(agentStatus([USER, m])).toEqual({ kind: "done", label: "Answered · 2 tools used" });
  });

  it("says just 'Answered' when no tools were used", () => {
    const m = asst({ streaming: false, stopReason: "end_turn", text: "hi" });
    expect(agentStatus([USER, m])).toEqual({ kind: "done", label: "Answered" });
  });

  it("surfaces guardrail/error stops", () => {
    const stop = (r: string) => agentStatus([USER, asst({ streaming: false, stopReason: r })]);
    expect(stop("budget")).toEqual({ kind: "error", label: "Stopped — hit the budget" });
    expect(stop("too_many_errors")).toEqual({
      kind: "error",
      label: "Stopped — tools kept failing",
    });
    expect(stop("error")).toEqual({ kind: "error", label: "Something went wrong" });
  });
});
