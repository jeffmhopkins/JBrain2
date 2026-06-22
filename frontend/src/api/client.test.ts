// The chat-turn glue on the client: the run id the PWA needs to Stop a detached
// turn rides as an `X-Run-Id` response header, which `api.chat` surfaces as a
// synthetic first `run` event (the detached turn no longer dies when the SSE stream
// closes, so Stop targets it by id via `cancelChatRun`). MOCK_MODE is off under
// vitest, so `request` uses the global fetch — stub it to drive these paths.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatEvent } from "../agent/types";
import { api } from "./client";

function sseBody(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
}

describe("api.chat run id", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("surfaces the X-Run-Id header as a synthetic run event ahead of the stream", async () => {
    const body = sseBody([
      'data: {"type":"text_delta","text":"hi"}\n\n',
      'data: {"type":"done","stop_reason":"end_turn"}\n\n',
    ]);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(body, { status: 200, headers: { "X-Run-Id": "run-42" } })),
    );

    const events: ChatEvent[] = [];
    for await (const event of api.chat({ session_id: "s1", message: "hi" })) events.push(event);

    // The run event leads, so the hook can capture the id before any content.
    expect(events[0]).toEqual({ type: "run", run_id: "run-42" });
    expect(events).toContainEqual({ type: "text_delta", text: "hi" });
    expect(events.at(-1)).toEqual({ type: "done", stop_reason: "end_turn" });
  });

  it("omits the run event when the header is absent", async () => {
    const body = sseBody(['data: {"type":"done","stop_reason":"end_turn"}\n\n']);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(body, { status: 200 })),
    );

    const events: ChatEvent[] = [];
    for await (const event of api.chat({ session_id: "s1", message: "hi" })) events.push(event);

    expect(events.some((e) => e.type === "run")).toBe(false);
  });

  it("cancelChatRun POSTs the run's cancel endpoint", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.cancelChatRun("run-9");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/chat/runs/run-9/cancel",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("chatResume GETs the run's stream from the given offset and parses events", async () => {
    const body = sseBody([
      'data: {"type":"text_delta","text":"more"}\n\n',
      'data: {"type":"done","stop_reason":"end_turn"}\n\n',
    ]);
    const fetchMock = vi.fn(async () => new Response(body, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const events: ChatEvent[] = [];
    for await (const event of api.chatResume("run-9", 3)) events.push(event);

    expect(fetchMock).toHaveBeenCalledWith("/api/chat/runs/run-9/stream?after=3", expect.anything());
    // No synthetic run event — the caller already holds the id; just the parsed frames.
    expect(events).toEqual([
      { type: "text_delta", text: "more" },
      { type: "done", stop_reason: "end_turn" },
    ]);
  });
});
