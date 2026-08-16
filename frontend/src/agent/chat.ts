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
    // CANCEL, not just releaseLock. Releasing the lock detaches the reader but leaves the
    // body — and therefore the HTTP connection — open. On the normal path the loop drains
    // to `done` and the connection closes anyway, but a turn that is Stopped, aborted, or
    // whose consumer unmounts mid-stream discards this generator, and that connection was
    // never torn down. Leaked sockets accumulate per turn and browsers cap them per
    // origin, so the least important stream on the page — the 1 Hz vitals meter — is the
    // one that stops being able to reconnect.
    //
    // Cancelling an already-finished body is a no-op, and a cancel that rejects (the body
    // is already errored) must not mask the real reason we are unwinding.
    try {
      await reader.cancel();
    } catch {
      // already closed or errored — nothing left to release
    }
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
