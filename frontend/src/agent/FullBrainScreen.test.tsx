import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FullBrainScreen, type TranscriptMessage, applyEvent } from "./FullBrainScreen";
import type { AgentSession, ChatEvent, ChatRequest } from "./types";

const SESSION: AgentSession = {
  id: "sess-1",
  title: "",
  status: "active",
  domain_scopes: ["general", "health"],
  subject_ids: [],
  created_at: "2026-06-12T00:00:00Z",
  last_active_at: "2026-06-12T00:00:00Z",
};

function streaming(): TranscriptMessage {
  return { role: "assistant", text: "", tools: [], views: [], streaming: true };
}

function fakeChat(events: ChatEvent[]): {
  fn: (body: ChatRequest) => AsyncGenerator<ChatEvent>;
  calls: ChatRequest[];
} {
  const calls: ChatRequest[] = [];
  async function* fn(body: ChatRequest): AsyncGenerator<ChatEvent> {
    calls.push(body);
    for (const e of events) yield e;
  }
  return { fn, calls };
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
    expect(turn?.tools).toEqual([{ id: "c1", name: "search", ok: true, summary: "found 3" }]);
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
});

describe("FullBrainScreen", () => {
  it("streams an assistant answer with tool activity into the transcript", async () => {
    const { fn } = fakeChat([
      { type: "text_delta", text: "checking" },
      { type: "tool_call", id: "c1", name: "search", arguments: { q: "labs" } },
      { type: "tool_result", tool_call_id: "c1", ok: true, summary: "2 notes" },
      { type: "done", stop_reason: "end_turn" },
    ]);
    render(<FullBrainScreen session={SESSION} chat={fn} />);

    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "what labs?" } });
    fireEvent.click(screen.getByLabelText("Send"));

    await waitFor(() => expect(screen.getByText("checking")).toBeInTheDocument());
    expect(screen.getByText("what labs?")).toBeInTheDocument();
    expect(screen.getByText("search · 2 notes")).toBeInTheDocument();
  });

  it("sends prior turns as history on the second message", async () => {
    const first = fakeChat([
      { type: "text_delta", text: "first answer" },
      { type: "done", stop_reason: "end_turn" },
    ]);
    const { rerender } = render(<FullBrainScreen session={SESSION} chat={first.fn} />);
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "first q" } });
    fireEvent.click(screen.getByLabelText("Send"));
    await waitFor(() => expect(screen.getByText("first answer")).toBeInTheDocument());

    const second = fakeChat([{ type: "done", stop_reason: "end_turn" }]);
    rerender(<FullBrainScreen session={SESSION} chat={second.fn} />);
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "second q" } });
    fireEvent.click(screen.getByLabelText("Send"));

    await waitFor(() => expect(second.calls).toHaveLength(1));
    expect(second.calls[0]?.history).toEqual([
      { role: "user", content: "first q" },
      { role: "assistant", content: "first answer" },
    ]);
    expect(second.calls[0]?.message).toBe("second q");
  });

  it("shows the session's read scope", () => {
    render(<FullBrainScreen session={SESSION} chat={fakeChat([]).fn} />);
    expect(screen.getByText("Full Brain · general · health")).toBeInTheDocument();
  });

  it("recovers from a stream error by closing the turn", async () => {
    async function* boom(): AsyncGenerator<ChatEvent> {
      yield { type: "text_delta", text: "partial" };
      throw new Error("dropped");
    }
    render(<FullBrainScreen session={SESSION} chat={boom} />);
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "hi" } });
    fireEvent.click(screen.getByLabelText("Send"));
    await waitFor(() => expect(screen.getByText("partial")).toBeInTheDocument());
    // The composer re-enables (busy cleared) after the error.
    await waitFor(() => {
      fireEvent.change(screen.getByLabelText("Message"), { target: { value: "again" } });
      expect(screen.getByLabelText("Send")).not.toBeDisabled();
    });
  });
});
