// The Full Brain transcript model and its streaming reducer. Pure so the reducer
// is unit-testable and shared between the hook (which drives it) and the view
// (which renders it). Text deltas accumulate into the live assistant bubble;
// tool calls/results become activity rows and tool_view payloads collect for the
// component registry.

import type { ChatEvent, EntityRef, ProposalRef, ViewPayload } from "./types";

/** A source note a tool surfaced, ready for a card: id to open, domain for the
 * dot, text for the line. */
export interface SourceRef {
  noteId: string;
  domain: string;
  text: string;
}

export interface ToolActivity {
  id: string;
  name: string;
  /** undefined while the call is in flight; set when its result arrives. */
  ok?: boolean;
  /** The arguments the call went out with — kept so a step can show what it
   * actually searched/read when its detail is expanded. */
  args?: Record<string, unknown>;
  summary?: string;
  /** Structured notes the tool surfaced, sent with the result event. */
  sources?: SourceRef[];
  /** A Proposal this tool staged (a "Review proposal" chip). */
  proposal?: ProposalRef;
  /** Entities this tool resolved (tappable chips). */
  entities?: EntityRef[];
}

/** Reflexion's verdict on this turn — present only when the verifiers flagged
 * something (a passing/absent verdict leaves the message unflagged). Drives the
 * inline "unverified" flags on the ungrounded answer sentences. */
export interface Verdict {
  passed: boolean;
  score: number;
  issues: string[];
  ungroundedClaims: string[];
}

export interface TranscriptMessage {
  role: "user" | "assistant";
  text: string;
  tools: ToolActivity[];
  views: ViewPayload[];
  streaming: boolean;
  stopReason?: string;
  /** The model's reasoning trace (gpt-oss/GLM), accumulated from `reasoning_delta`
   * and replayed from storage. Empty for non-reasoning turns. */
  reasoning: string;
  /** True while reasoning is streaming and the answer hasn't started — drives the
   * live "Thinking…" state; flips false on the first answer token (or `done`). */
  thinking: boolean;
  /** Reflexion's flag on this turn — absent until a `verdict` event lands. */
  verdict?: Verdict;
  /** Neutral provenance: the turn answered from the model's own knowledge with no
   * retrieval — set when a `general_knowledge` event lands (mutually exclusive with
   * `verdict`). Drives the calm "not your notes" footer chip. */
  generalKnowledge?: boolean;
}

export function userMessage(text: string): TranscriptMessage {
  return {
    role: "user",
    text,
    tools: [],
    views: [],
    streaming: false,
    reasoning: "",
    thinking: false,
  };
}

export function streamingAssistant(): TranscriptMessage {
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

/** Fold one ChatEvent into the transcript, updating the live assistant turn (the
 * last message). */
export function applyEvent(messages: TranscriptMessage[], event: ChatEvent): TranscriptMessage[] {
  const last = messages[messages.length - 1];
  if (!last || last.role !== "assistant") return messages;
  const next: TranscriptMessage = { ...last };
  switch (event.type) {
    case "text_delta":
      next.text += event.text;
      // The answer has begun — the thinking phase is over (collapse the disclosure).
      next.thinking = false;
      break;
    case "reasoning_delta":
      next.reasoning += event.text;
      // Live "thinking" only until the answer's first token; later reasoning (a
      // multi-step turn) appends to the trace without reopening the disclosure.
      next.thinking = next.text === "";
      break;
    case "tool_call":
      next.tools = [
        ...next.tools,
        {
          id: event.id,
          name: event.name,
          // Keep the arguments only when there are some — an empty object is noise
          // in the expanded detail.
          ...(Object.keys(event.arguments).length ? { args: event.arguments } : {}),
        },
      ];
      break;
    case "tool_result": {
      const sources = (event.sources ?? []).map((s) => ({
        noteId: s.note_id,
        domain: s.domain,
        text: s.snippet,
      }));
      const extra = {
        ...(event.proposal ? { proposal: event.proposal } : {}),
        ...(event.entities?.length ? { entities: event.entities } : {}),
      };
      next.tools = next.tools.map((t) =>
        t.id === event.tool_call_id
          ? { ...t, ok: event.ok, summary: event.summary, sources, ...extra }
          : t,
      );
      break;
    }
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
      // The turn settled — a reasoning-only turn (no answer text) stops thinking now.
      next.thinking = false;
      break;
    case "verdict":
      // Rides after `done` (Loop 1's annotation). Attach it to the just-settled
      // turn; the bubble renders inline "unverified" flags when it isn't a pass.
      next.verdict = {
        passed: event.passed,
        score: event.score,
        issues: event.issues ?? [],
        ungroundedClaims: event.ungrounded_claims ?? [],
      };
      break;
    case "general_knowledge":
      // Rides after `done`, like the verdict — but neutral. The backend guarantees
      // it never co-occurs with a verdict, so the bubble shows at most one footer.
      next.generalKnowledge = true;
      break;
  }
  return [...messages.slice(0, -1), next];
}

export function endStream(messages: TranscriptMessage[], reason: string): TranscriptMessage[] {
  return applyEvent(messages, { type: "done", stop_reason: reason });
}
