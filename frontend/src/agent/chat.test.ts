import { describe, expect, it } from "vitest";
import { parseChatStream } from "./chat";
import type { ChatEvent } from "./types";

/** A ReadableStream that emits the given byte chunks — lets a test split SSE
 * frames across reads, the way the network does. */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(stream: ReadableStream<Uint8Array>): Promise<ChatEvent[]> {
  const out: ChatEvent[] = [];
  for await (const event of parseChatStream(stream)) out.push(event);
  return out;
}

describe("parseChatStream", () => {
  it("parses text deltas, a tool round-trip, and done in order", async () => {
    const events = await collect(
      streamOf([
        'data: {"type": "text_delta", "text": "let me "}\n\n',
        'data: {"type": "text_delta", "text": "check"}\n\n',
        'data: {"type": "tool_call", "id": "c1", "name": "search", "arguments": {"q": "x"}}\n\n',
        'data: {"type": "tool_result", "tool_call_id": "c1", "ok": true, "summary": "found"}\n\n',
        'data: {"type": "done", "stop_reason": "end_turn"}\n\n',
      ]),
    );
    expect(events).toEqual([
      { type: "text_delta", text: "let me " },
      { type: "text_delta", text: "check" },
      { type: "tool_call", id: "c1", name: "search", arguments: { q: "x" } },
      { type: "tool_result", tool_call_id: "c1", ok: true, summary: "found" },
      { type: "done", stop_reason: "end_turn" },
    ]);
  });

  it("reassembles an event split across two reads", async () => {
    const events = await collect(streamOf(['data: {"type": "text_', 'delta", "text": "hi"}\n\n']));
    expect(events).toEqual([{ type: "text_delta", text: "hi" }]);
  });

  it("skips a malformed frame without dropping the events after it", async () => {
    const events = await collect(
      streamOf(["data: {not json}\n\n", 'data: {"type": "done", "stop_reason": "end_turn"}\n\n']),
    );
    expect(events).toEqual([{ type: "done", stop_reason: "end_turn" }]);
  });

  it("ignores non-data lines and blank frames", async () => {
    const events = await collect(
      streamOf([": keep-alive\n\n", 'data: {"type": "text_delta", "text": "ok"}\n\n']),
    );
    expect(events).toEqual([{ type: "text_delta", text: "ok" }]);
  });
});
