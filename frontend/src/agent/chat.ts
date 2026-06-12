// Parse the /api/chat SSE byte stream into ChatEvents. The server frames each
// event as `data: <json>\n\n` (api/agent.py); we split on the blank-line
// boundary, decode each `data:` line, and yield the parsed event. A malformed
// frame is skipped rather than aborting the turn — a dropped event must not
// swallow the ones after it.

import type { ChatEvent } from "./types";

export async function* parseChatStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<ChatEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseFrame(frame);
        if (event) yield event;
        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseFrame(frame: string): ChatEvent | null {
  const line = frame.split("\n").find((l) => l.startsWith("data:"));
  if (!line) return null;
  const json = line.slice("data:".length).trim();
  if (!json) return null;
  try {
    return JSON.parse(json) as ChatEvent;
  } catch {
    return null;
  }
}
