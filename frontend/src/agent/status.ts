// Derive a one-line "what's the agent doing right now" status from the live
// transcript, for the indicator above the composer. Pure so it's unit-testable;
// the view (AgentStatusLine in FullBrainSurface) just renders the result.

import type { TranscriptMessage } from "./transcript";

export type AgentStatusKind = "thinking" | "tool" | "answering" | "done" | "error";

export interface AgentStatus {
  kind: AgentStatusKind;
  /** Plain leading text. */
  label: string;
  /** Emphasised tail — the tool's object ("your notes"), rendered stronger. */
  emphasis?: string;
}

// Friendly verb + object per tool (names come from the agent registry). Anything
// unmapped falls back to a generic so a new tool still reads sensibly.
const TOOL_LABELS: Record<string, { label: string; emphasis?: string }> = {
  search: { label: "Searching", emphasis: "your notes" },
  read_note: { label: "Reading", emphasis: "a note" },
  read_entity: { label: "Reading", emphasis: "an entity" },
  recall: { label: "Recalling", emphasis: "past notes" },
  memory_read: { label: "Reading", emphasis: "memory" },
  memory_edit: { label: "Updating", emphasis: "its scratchpad" },
  remember: { label: "Staging", emphasis: "a memory change" },
  propose_correction: { label: "Staging", emphasis: "a proposal" },
};

function toolStatus(name: string): AgentStatus {
  const mapped = TOOL_LABELS[name];
  if (mapped) {
    return mapped.emphasis
      ? { kind: "tool", label: mapped.label, emphasis: mapped.emphasis }
      : { kind: "tool", label: mapped.label };
  }
  // Connector tools are registered as `lookup_<thing>`; show the thing.
  if (name.startsWith("lookup_")) {
    return { kind: "tool", label: "Checking", emphasis: name.slice(7).replace(/_/g, " ") };
  }
  return { kind: "tool", label: "Using", emphasis: name };
}

// A turn that stopped for a guardrail/error reason rather than finishing cleanly.
const STOP_LABELS: Record<string, string> = {
  budget: "Stopped — hit the budget",
  max_steps: "Stopped — too many steps",
  too_many_errors: "Stopped — tools kept failing",
  error: "Something went wrong",
};

/** The current agent status, or null when idle (nothing to show). Reads only the
 * live (last) assistant turn. */
export function agentStatus(messages: TranscriptMessage[]): AgentStatus | null {
  const last = messages[messages.length - 1];
  if (!last || last.role !== "assistant") return null;

  if (last.streaming) {
    const running = last.tools.find((t) => t.ok === undefined);
    if (running) return toolStatus(running.name);
    if (last.text) return { kind: "answering", label: "Writing the answer" };
    return { kind: "thinking", label: "Thinking it through" };
  }

  const stop = last.stopReason;
  if (stop && STOP_LABELS[stop]) return { kind: "error", label: STOP_LABELS[stop] };
  // Clean finish — a quiet confirmation with how many tools it used.
  const used = last.tools.filter((t) => t.name !== "queued").length;
  return {
    kind: "done",
    label: used ? `Answered · ${used} tool${used > 1 ? "s" : ""} used` : "Answered",
  };
}
