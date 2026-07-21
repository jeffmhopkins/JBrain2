// The Full Brain transcript model and its streaming reducer. Pure so the reducer
// is unit-testable and shared between the hook (which drives it) and the view
// (which renders it). Text deltas accumulate into the live assistant bubble;
// tool calls/results become activity rows and tool_view payloads collect for the
// component registry.

import type {
  ChatAttachment,
  ChatEvent,
  EntityRef,
  ProposalRef,
  ViewPayload,
  WebSource,
} from "./types";

/** A source note a tool surfaced, ready for a card: id to open, domain for the
 * dot, text for the line. */
export interface SourceRef {
  noteId: string;
  domain: string;
  text: string;
}

/** One sub-agent child in a `spawn_subagent` fan, folded from the `subagent_*`
 * events (docs/archive/SUBAGENT_SPAWNING_PLAN.md, Wave S3). Persona is a neutral tag; the
 * glyph colour comes from `status` (steel=running, green=done, rose=failed). */
export interface SubagentChild {
  childId: string;
  persona: string;
  label: string;
  depth: number;
  /** The live working word while running ("researching"), the stop reason once settled. */
  phase: string;
  status: "running" | "done" | "failed";
  /** The child's ReAct step so far — shown as live movement ("· N steps") while running. */
  step?: number;
  /** The child's answer (or its error / truncation note), shown on expand. */
  summary?: string;
  stopReason?: string;
  /** Live-streamed answer tokens while the child works (cleared/superseded by
   * `summary` once it settles) — the fan's live mini-transcript. */
  liveText?: string;
  /** The child's reasoning AND tool calls in arrival order — one interleaved stream so
   * the fan can render a tool call where it happened inside the thinking (heavy tool use
   * folds away with the collapsible trace instead of piling into a separate flat list). */
  liveTrace?: SubagentTraceItem[];
  /** The child's live context fill (latest model call's prompt+output) and its model's
   * window — drives the per-row context meter. Set from `subagent_usage`; absent until
   * the child's first model call reports usage. */
  usedTokens?: number;
  contextWindow?: number;
  /** Staged-fan placement (feeding waves): the child's wave (0-based; 0/undefined for a
   * flat fan) and the earlier-wave producers fed into it — so the live fan groups rows by
   * wave and draws the "← fed by …" edge as it runs, not only in the final synthesis card. */
  wave?: number;
  fedFrom?: string[];
  /** The deep_research pipeline stage that spawned this child (1-based checklist ordinal:
   * 2=Research, 3=Cross-check, 5=Gap-fill, 7=Critique; 0/undefined for a plain fan). The
   * checklist nests each child under the stage it ran in, rather than piling every stage's
   * children under whichever stage is currently live. */
  drStage?: number;
}

/** One tool step a child took (web_search/web_fetch), shown in the child's frame. */
export interface SubagentToolStep {
  name: string;
  arg: string;
  ok: boolean;
}

/** One item in a child's interleaved live trace: a run of reasoning text, or a tool
 * call injected at the point it occurred. */
export type SubagentTraceItem =
  | { kind: "reasoning"; text: string }
  | ({ kind: "tool" } & SubagentToolStep);

/** The live fan under a `spawn_subagent` tool call: its children plus the shared
 * tree-budget snapshot driving the in-chat meter. */
export interface SubagentFan {
  children: SubagentChild[];
  treeSpent: number;
  treeBudget: number;
}

export interface ToolActivity {
  id: string;
  name: string;
  /** undefined while the call is in flight; set when its result arrives. */
  ok?: boolean;
  /** A sub-agent fan this call launched — present only on a `spawn_subagent` step. */
  fan?: SubagentFan;
  /** The arguments the call went out with — kept so a step can show what it
   * actually searched/read when its detail is expanded. */
  args?: Record<string, unknown>;
  summary?: string;
  /** Structured notes the tool surfaced, sent with the result event. */
  sources?: SourceRef[];
  /** Web pages a jerv internet tool reached — favicon citation chips. */
  webSources?: WebSource[];
  /** A Proposal this tool staged (a "Review proposal" chip). */
  proposal?: ProposalRef;
  /** Entities this tool resolved (tappable chips). */
  entities?: EntityRef[];
  /** Live progress for an in-flight tool — image gen's sampler step/total + sharpening
   * preview, or a multi-phase tool's text `label` ("Analyzing frame 12/30"). Set by
   * `tool_progress`, cleared when the result lands (the final view then renders). */
  progress?: { step: number; total: number; preview?: string; label?: string };
  /** The last live preview frame, kept after the result settles so the final image
   * view can show it as a placeholder until the full-res image loads — no blank gap
   * between "finalizing" and the rendered image. Live-only (absent on reopen). */
  preview?: string;
  /** The answer-text length when this tool was called — the point the turn's prose
   * splits around it. An image turn uses it to render preamble → image → reply as
   * three messages; set live and persisted so reopen splits the same way. */
  textOffset?: number;
  /** The reasoning-trace length when this tool was called — the point it falls inside
   * the thinking. The "Thinking" disclosure interleaves the call there (like a
   * sub-agent's trace); set live and persisted so reopen interleaves the same way. */
  reasoningOffset?: number;
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
  /** Files the owner attached to this (user) turn — rendered as compact chips
   * inside the bubble, above the text. Empty/absent on assistant turns. */
  attachments?: ChatAttachment[];
}

export function userMessage(text: string, attachments?: ChatAttachment[]): TranscriptMessage {
  return {
    role: "user",
    text,
    tools: [],
    views: [],
    streaming: false,
    reasoning: "",
    thinking: false,
    ...(attachments?.length ? { attachments } : {}),
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

/** Append a run of reasoning text to a child's interleaved trace, coalescing into the
 * trailing reasoning item so consecutive deltas read as one paragraph (a tool call
 * between them is what starts a fresh reasoning run). */
function appendReasoning(
  trace: SubagentTraceItem[] | undefined,
  text: string,
): SubagentTraceItem[] {
  const items = trace ?? [];
  const last = items[items.length - 1];
  if (last && last.kind === "reasoning") {
    return [...items.slice(0, -1), { kind: "reasoning", text: last.text + text }];
  }
  return [...items, { kind: "reasoning", text }];
}

/** A child referenced by a `subagent_*` event before (or without) its `subagent_spawned`
 * frame — a reconnect that resumed mid-fan, or a frame evicted from the live buffer.
 * Rather than drop the row, materialize a minimal placeholder; `subagent_spawned` upserts
 * the real persona/label by child_id if/when it arrives. */
function placeholderChild(childId: string): SubagentChild {
  return {
    childId,
    persona: "research",
    label: "sub-agent",
    depth: 1,
    phase: "working…",
    status: "running",
  };
}

/** Apply `fn` to child `childId` under the `toolCallId` spawn step, LAZILY creating the
 * fan and a placeholder child if missing — so a `subagent_*` event whose `subagent_spawned`
 * never arrived (reconnect / evicted frame) updates a real row instead of silently
 * no-op'ing (the old handlers gated on `t.fan` && an existing child and dropped it). */
function withFanChild(
  tools: ToolActivity[],
  toolCallId: string,
  childId: string,
  fn: (c: SubagentChild) => SubagentChild,
  fanPatch?: Partial<Pick<SubagentFan, "treeSpent" | "treeBudget">>,
): ToolActivity[] {
  return tools.map((t) => {
    if (t.id !== toolCallId) return t;
    const fan = t.fan ?? { children: [], treeSpent: 0, treeBudget: 0 };
    const has = fan.children.some((c) => c.childId === childId);
    const children = has
      ? fan.children.map((c) => (c.childId === childId ? fn(c) : c))
      : [...fan.children, fn(placeholderChild(childId))];
    return { ...t, fan: { ...fan, ...fanPatch, children } };
  });
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
          // The prose streamed so far is this call's preamble; record where it ends so
          // an image turn can split into preamble → image → reply (see FullBrainSurface).
          textOffset: next.text.length,
          // The reasoning streamed so far is where this call falls in the thinking; record
          // it so the "Thinking" disclosure interleaves the tool there (see ActivityLine).
          reasoningOffset: next.reasoning.length,
          // Keep the arguments only when there are some — an empty object is noise
          // in the expanded detail.
          ...(Object.keys(event.arguments).length ? { args: event.arguments } : {}),
        },
      ];
      break;
    case "tool_progress":
      // Update the in-flight tool's live preview; ignored if its call isn't known
      // yet (the tool_call always precedes its progress on the wire).
      next.tools = next.tools.map((t) =>
        t.id === event.tool_call_id
          ? {
              ...t,
              progress: {
                step: event.step,
                total: event.total,
                ...(event.preview ? { preview: event.preview } : {}),
                ...(event.label ? { label: event.label } : {}),
              },
            }
          : t,
      );
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
        ...(event.web_sources?.length ? { webSources: event.web_sources } : {}),
      };
      next.tools = next.tools.map((t) => {
        if (t.id !== event.tool_call_id) return t;
        // The result settled, so the live progress goes — but carry the last preview
        // frame forward so the final image view can hold it as a placeholder until the
        // full-res image loads (no blank gap between "finalizing" and the render).
        const { progress, ...rest } = t;
        const carried = progress?.preview ? { preview: progress.preview } : {};
        return { ...rest, ok: event.ok, summary: event.summary, sources, ...carried, ...extra };
      });
      break;
    }
    case "tool_view": {
      // A subagent_synthesis view is a SUPERSEDING roster: the fan re-emits it as each
      // child settles, then once more as the final result — all stamped with the same
      // spawn tool_call_id. Replace the prior roster for that call instead of stacking
      // every update as its own card (which showed "1 of 1", "2 of 2", "2 of 2" live; the
      // backend accumulator already supersedes, which is why a reopened session shows
      // one). Keyed by tool_call_id so a turn with two separate fans still shows two cards.
      if (event.view.view === "subagent_synthesis") {
        const tagged = { ...event.view, tool_call_id: event.tool_call_id };
        const kept = next.views.filter(
          (v) => !(v.view === "subagent_synthesis" && v.tool_call_id === event.tool_call_id),
        );
        next.views = [...kept, tagged];
      } else {
        next.views = [...next.views, event.view];
      }
      break;
    }
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
      // Settle any sub-agent still shown as running. A turn that ended — especially a
      // Stop/cancel, which cascades CancelledError into the fan and so emits no
      // per-child `subagent_done` — must not leave a child bouncing "running" forever.
      next.tools = next.tools.map((t) =>
        t.fan?.children.some((c) => c.status === "running")
          ? {
              ...t,
              fan: {
                ...t.fan,
                children: t.fan.children.map((c) =>
                  c.status === "running"
                    ? { ...c, status: "failed", phase: "cancelled", stopReason: "cancelled" }
                    : c,
                ),
              },
            }
          : t,
      );
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
    case "subagent_spawned":
      // Attach (or upgrade) a child row on its spawn_subagent tool call. Upsert by
      // child_id: a reconnect replay can't double it, and a placeholder a stray
      // progress/delta already created gets its real persona/label filled in here. Only a
      // never-progressed row resets to "queued" (don't drag a working child back).
      next.tools = withFanChild(next.tools, event.tool_call_id, event.child_id, (c) => ({
        ...c,
        persona: event.persona,
        label: event.label,
        depth: event.depth,
        phase: c.step ? c.phase : "queued",
        wave: event.wave ?? 0,
        fedFrom: event.fed_from ?? [],
        drStage: event.dr_stage ?? 0,
      }));
      break;
    case "subagent_progress":
      next.tools = withFanChild(
        next.tools,
        event.tool_call_id,
        event.child_id,
        (c) => ({ ...c, phase: event.phase, status: "running", step: event.step }),
        { treeSpent: event.tree_spent, treeBudget: event.tree_budget },
      );
      break;
    case "subagent_usage":
      // The child's live context fill — drives its per-row context meter. Folds onto the
      // matching child without touching its phase/step (a separate tick from progress).
      next.tools = next.tools.map((t) =>
        t.id === event.tool_call_id && t.fan
          ? {
              ...t,
              fan: {
                ...t.fan,
                children: t.fan.children.map((c) =>
                  c.childId === event.child_id
                    ? { ...c, usedTokens: event.used, contextWindow: event.context_window }
                    : c,
                ),
              },
            }
          : t,
      );
      break;
    case "subagent_delta":
      // The child's live tokens — reasoning folds into the interleaved trace (so a tool
      // call lands where it happened), the answer accumulates separately. It runs
      // non-streaming from the parent's await but forwards its tokens through the sink.
      next.tools = withFanChild(next.tools, event.tool_call_id, event.child_id, (c) =>
        event.channel === "reasoning"
          ? { ...c, liveTrace: appendReasoning(c.liveTrace, event.text) }
          : { ...c, liveText: (c.liveText ?? "") + event.text },
      );
      break;
    case "subagent_tool":
      // Inject the tool call into the trace at the point it occurred — interleaved with
      // the reasoning, not a separate flat list (a 20-search child stays readable).
      next.tools = withFanChild(next.tools, event.tool_call_id, event.child_id, (c) => ({
        ...c,
        liveTrace: [
          ...(c.liveTrace ?? []),
          { kind: "tool", name: event.name, arg: event.arg, ok: event.ok },
        ],
      }));
      break;
    case "subagent_done":
      next.tools = withFanChild(
        next.tools,
        event.tool_call_id,
        event.child_id,
        (c) => ({
          ...c,
          status: event.ok ? "done" : "failed",
          phase: event.stop_reason,
          stopReason: event.stop_reason,
          summary: event.summary,
        }),
        { treeSpent: event.tree_spent, treeBudget: event.tree_budget },
      );
      break;
  }
  return [...messages.slice(0, -1), next];
}

export function endStream(messages: TranscriptMessage[], reason: string): TranscriptMessage[] {
  return applyEvent(messages, { type: "done", stop_reason: reason });
}
