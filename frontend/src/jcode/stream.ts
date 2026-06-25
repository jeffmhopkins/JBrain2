// Parse the jcode turn SSE byte stream into JcodeEvents — the same `data: <json>\n\n`
// framing as /api/chat (api/jcode.py), so this mirrors agent/chat.ts parseChatStream.
// A malformed frame is skipped, never aborting the turn.

import type { JcodeEvent } from "./types";

export async function* parseJcodeStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<JcodeEvent> {
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

function parseFrame(frame: string): JcodeEvent | null {
  const line = frame.split("\n").find((l) => l.startsWith("data:"));
  if (!line) return null;
  const json = line.slice("data:".length).trim();
  if (!json) return null;
  try {
    return JSON.parse(json) as JcodeEvent;
  } catch {
    return null;
  }
}
