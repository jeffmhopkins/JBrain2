// Derive a one-line "what's the agent doing right now" status from the live
// transcript, for the indicator above the composer. Pure so it's unit-testable;
// the view (AgentStatusLine in FullBrainSurface) just renders the result.

import type { TranscriptMessage } from "./transcript";

export type AgentStatusKind =
  | "thinking"
  | "tool"
  | "answering"
  | "done"
  | "error"
  | "waiting"
  | "loading";

export interface AgentStatus {
  kind: AgentStatusKind;
  /** Plain leading text. */
  label: string;
  /** Emphasised tail — the tool's object ("your notes"), rendered stronger. On a
   * `waiting` status this carries the live countdown (e.g. "0:54"); on a `loading` one,
   * the name of the model coming onto the box. */
  emphasis?: string;
  /** `loading` only: the fraction of the weights read in (0..1), or null when the gateway
   * gives no parseable figure — rendered as a bare "loading…" rather than as 0%. */
  percent?: number | null;
  /** `loading` only: when the load started, so the line counts up from the LOAD's own
   * clock. The view's phase timer anchors to the phase it replaced (a turn that has been
   * "thinking" for two minutes), which would open this line mid-count on a load that
   * began a second ago. */
  sinceMs?: number;
  /** Identity of the live turn, so the status line can reset its per-turn timer at a
   * turn boundary. Steady across a turn's phases (thinking → tool → answering) and
   * distinct across turns — a new turn appends messages, so the message count changes
   * exactly there. Scoped by the session id so it is also distinct ACROSS conversations:
   * the message-index alone collides (every conversation's first turn is index 1), which
   * left the timer anchored to a prior conversation's start when the status line (which
   * persists across conversation switches) failed to re-anchor. Absent only when a status
   * is built by hand (tests). */
  turnKey?: string;
}

// Friendly verb + object per tool (names come from the agent registry). Anything
// unmapped falls back to a generic so a new tool still reads sensibly.
const TOOL_LABELS: Record<string, { label: string; emphasis?: string }> = {
  search: { label: "Searching", emphasis: "your notes" },
  read_note: { label: "Reading", emphasis: "a note" },
  read_entity: { label: "Reading", emphasis: "an entity" },
  find_entity: { label: "Looking up", emphasis: "an entity" },
  relate: { label: "Following", emphasis: "a relationship" },
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
  context_overflow:
    "This model ran out of context — raise its window in Settings or start a new chat",
  error: "Something went wrong",
};

/** The status of the live turn's current phase, without its turn identity. */
function phaseStatus(last: TranscriptMessage): AgentStatus {
  if (last.streaming) {
    const running = last.tools.find((t) => t.ok === undefined);
    // A multi-phase tool streams a live phase label ("Analyzing frame 12/30") — show
    // that instead of the generic verb so the owner sees real progress, not a freeze.
    if (running?.progress?.label) return { kind: "tool", label: running.progress.label };
    if (running) return toolStatus(running.name);
    if (last.text) return { kind: "answering", label: "Writing the answer" };
    return { kind: "thinking", label: "Thinking it through" };
  }

  const stop = last.stopReason;
  // A user-initiated Stop is calm (a "done" register), not the red error the
  // guardrail/error reasons get.
  if (stop === "stopped") return { kind: "done", label: "Stopped" };
  if (stop && STOP_LABELS[stop]) return { kind: "error", label: STOP_LABELS[stop] };
  // Clean finish — a quiet confirmation with how many tools it used.
  const used = last.tools.filter((t) => t.name !== "queued").length;
  return {
    kind: "done",
    label: used ? `Answered · ${used} tool${used > 1 ? "s" : ""} used` : "Answered",
  };
}

/** The between-steps status for a working plan: while a continuation is armed but not yet
 * firing, the status line shows this instead of the settled "Answered" — an owner-visible,
 * interruptible countdown to the next auto-step (JERV_PLANNING_TOOL_PLAN.md §2 issue). The
 * countdown ("m:ss") rides `emphasis` so the view renders it emphasised, like a tool object.
 * Built here (not by `agentStatus`, which reads only the transcript) and passed in by the
 * surface, which owns the live plan state. */
export function planWaitingStatus(countdown: string): AgentStatus {
  return { kind: "waiting", label: "Starting next step in", emphasis: countdown };
}

/** What the BOX is doing, when that outranks what the turn is doing: a model is being
 * read into memory, and until it is there no turn can move.
 *
 * This is the line's honesty fix. A cold 120B takes the better part of a minute to load,
 * and for every second of it the transcript-derived status said "Thinking it through" with
 * a climbing timer — the agent looking hung, while the box was in fact doing the heaviest
 * work it ever does. Built here rather than by `agentStatus`, which reads only the
 * transcript, and passed in by the surface (like the plan countdown) because the load is a
 * property of the box, not of the conversation: it shows with no turn running at all, which
 * is exactly the case where nothing else on the chat screen would explain the wait.
 *
 * `model` is the served name the box knows it by — the same string the vitals event list
 * shows — so the owner reading both surfaces sees one name, not two. */
export function modelLoadStatus(
  model: string,
  percent: number | null,
  sinceMs: number,
): AgentStatus {
  return { kind: "loading", label: "Loading", emphasis: model, percent, sinceMs };
}

/** The current agent status, or null when idle (nothing to show). Reads only the
 * live (last) assistant turn. `sessionId` scopes the turn identity to the conversation
 * so the timer re-anchors when you switch chats (see `turnKey`). */
export function agentStatus(messages: TranscriptMessage[], sessionId?: string): AgentStatus | null {
  const last = messages[messages.length - 1];
  if (!last || last.role !== "assistant") return null;
  // Tag the phase with the turn's identity so the view resets its turn timer at each
  // boundary. The last message's index is steady across the turn and bumps when the
  // next turn's user+assistant pair is appended; the session id keeps two conversations'
  // same-index turns from colliding.
  return { ...phaseStatus(last), turnKey: `${sessionId ?? ""}#${messages.length - 1}` };
}
