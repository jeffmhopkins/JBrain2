// The Full Brain transcript model and its streaming reducer. Pure so the reducer
// is unit-testable and shared between the hook (which drives it) and the view
// (which renders it). Text deltas accumulate into the live assistant bubble;
// tool calls/results become activity rows and tool_view payloads collect for the
// component registry.

import type { ChatEvent, ViewPayload } from "./types";

export interface ToolActivity {
  id: string;
  name: string;
  /** undefined while the call is in flight; set when its result arrives. */
  ok?: boolean;
  summary?: string;
}

export interface TranscriptMessage {
  role: "user" | "assistant";
  text: string;
  tools: ToolActivity[];
  views: ViewPayload[];
  streaming: boolean;
  stopReason?: string;
}

export function userMessage(text: string): TranscriptMessage {
  return { role: "user", text, tools: [], views: [], streaming: false };
}

export function streamingAssistant(): TranscriptMessage {
  return { role: "assistant", text: "", tools: [], views: [], streaming: true };
}

/** Fold one ChatEvent into the transcript, updating the live assistant turn (the
 * last message). */
export function applyEvent(messages: TranscriptMessage[], event: ChatEvent): TranscriptMessage[] {
  const last = messages[messages.length - 1];
  if (!last || last.role !== "assistant") return messages;
  const next: TranscriptMessage = { ...last };
  switch (event.type) {
    case "text_delta":
      next.text += event.text;
      break;
    case "tool_call":
      next.tools = [...next.tools, { id: event.id, name: event.name }];
      break;
    case "tool_result":
      next.tools = next.tools.map((t) =>
        t.id === event.tool_call_id ? { ...t, ok: event.ok, summary: event.summary } : t,
      );
      break;
    case "tool_view":
      next.views = [...next.views, event.view];
      break;
    case "job_enqueued":
      next.tools = [
        ...next.tools,
        { id: event.job_id, name: "queued", ok: true, summary: event.summary },
      ];
      break;
    case "done":
      next.streaming = false;
      next.stopReason = event.stop_reason;
      break;
  }
  return [...messages.slice(0, -1), next];
}

export function endStream(messages: TranscriptMessage[], reason: string): TranscriptMessage[] {
  return applyEvent(messages, { type: "done", stop_reason: reason });
}
