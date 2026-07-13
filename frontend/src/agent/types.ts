// The agent chat wire shapes, mirroring backend agent/contracts.py. These cross
// the SSE wire from /api/chat (ChatEvent) and carry the tool-result views the
// component registry renders (ViewPayload / CitationRef). Hand-written until
// OpenAPI generation lands (docs/reference/DEVELOPMENT.md "TypeScript").

export type Domain = "general" | "health" | "finance" | "location";
export type Surface = "inline" | "sheet" | "dialog";

export interface FactRef {
  kind: "fact";
  fact_id: string;
  label: string;
}
export interface EntityRef {
  kind: "entity";
  entity_id: string;
  label: string;
  domain: Domain;
  /** Other surface forms (aka) — used to linkify a prose name that isn't the
   * canonical label (e.g. "Jeff Hopkins" for an entity canonically "Me"). */
  aliases?: string[];
  /** The entity's current-fact statements as read (read_entity only) — backend uses
   * them to ground a fact-value answer; the UI doesn't render them (the same prose is
   * already in the Worked step). Empty for find_entity/relate and related-object chips. */
  facts?: string[];
}
export interface NoteRef {
  kind: "note";
  note_id: string;
  label: string;
}
export type CitationRef = FactRef | EntityRef | NoteRef;

/** A tool result's rich UI: a registered component name + data-only slots. Never
 * model-authored markup — an unknown `view` renders nothing (DESIGN.md). */
export interface ViewPayload {
  view: string;
  surface: Surface;
  data: Record<string, unknown>;
  refs: CitationRef[];
  /** The originating tool call, stamped onto the view as the live reducer folds it.
   * Only used to SUPERSEDE a re-emitted view (the subagent_synthesis roster the fan
   * re-sends as each child settles) so updates replace rather than stack. Absent on a
   * reopened transcript (persisted views need no dedup — one per spawn step). */
  tool_call_id?: string;
}

// --- Streaming chat events (the /chat SSE union, discriminated on `type`) ---

export interface TextDelta {
  type: "text_delta";
  text: string;
}
/** A slice of the model's streamed reasoning trace (gpt-oss/GLM). Renders into the
 * collapsible "thinking" disclosure; never part of the answer. */
export interface ReasoningDelta {
  type: "reasoning_delta";
  text: string;
}
export interface ToolCallEvent {
  type: "tool_call";
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}
/** A note a read tool surfaced this turn, for the response's source cards. */
export interface NoteSource {
  note_id: string;
  domain: string;
  snippet: string;
}
/** A web page a jerv internet tool reached this turn — the real URL it surfaced
 * (a search hit / the fetched page), rendered as a tappable favicon citation chip.
 * The favicon is served on-box from `/api/agent/favicon` (the client never touches
 * the third-party host). */
export interface WebSource {
  url: string;
  title: string;
}
/** A Proposal a tool staged this turn — surfaced as a "Review proposal" chip. */
export interface ProposalRef {
  proposal_id: string;
  kind: string;
}
export interface ToolResultEvent {
  type: "tool_result";
  tool_call_id: string;
  ok: boolean;
  summary: string;
  sources?: NoteSource[];
  web_sources?: WebSource[];
  proposal?: ProposalRef | null;
  entities?: EntityRef[];
}
export interface ToolViewEvent {
  type: "tool_view";
  tool_call_id: string;
  view: ViewPayload;
}
/** A progress tick a still-running tool emitted (image generation): the sampler
 * step + total and an optional sharpening preview (a base-64 data URI the backend
 * authored). Ephemeral — drives the live in-chat preview, never persisted. */
export interface ToolProgressEvent {
  type: "tool_progress";
  tool_call_id: string;
  step: number;
  total: number;
  preview?: string | null;
  /** A human phase for a multi-step tool ("Analyzing frame 12/30"); image gen omits it. */
  label?: string | null;
}
export interface JobEnqueuedEvent {
  type: "job_enqueued";
  job_id: string;
  summary: string;
}
/** Live context-window accounting — rides after each model turn (every ReAct step)
 * so the composer can show a "context used" meter. `input_tokens` is the prompt the
 * model just consumed (the fullest the context has been this turn); `context_window`
 * is the resolved model's total window (a local model's gateway `-c`). Display only,
 * never persisted. */
export interface UsageEvent {
  type: "usage";
  input_tokens: number;
  output_tokens: number;
  context_window: number;
}
export interface DoneEvent {
  type: "done";
  stop_reason: string;
}
/** The server's run id for the in-flight turn, surfaced by `api.chat` as a synthetic
 * first event (read from the X-Run-Id header) so the composer's Stop can cancel the
 * turn server-side — the turn now runs detached from the SSE connection, so closing
 * the stream no longer stops it. Captured by the hook; never rendered, never persisted. */
export interface RunEvent {
  type: "run";
  run_id: string;
}
/** Reflexion's Loop-1 verdict on a critique-worthy turn — rides *after* `done`,
 * only when the verifiers flagged something (a passing turn emits none). The PWA
 * renders an "unverified claims" flag when `passed` is false; `ungrounded_claims`
 * are the verbatim answer sentences to anchor each flag against (the structured
 * twin of the grounding `issues`). Ephemeral — never persisted. */
export interface VerdictEvent {
  type: "verdict";
  passed: boolean;
  score: number;
  issues?: string[];
  ungrounded_claims?: string[];
}
/** Neutral provenance label — rides *after* `done` when a turn was answered purely
 * from the model's own world knowledge (zero retrieval) with a substantive claim.
 * NOT a warning: it carries no claims and never co-occurs with `verdict` (a turn
 * that retrieved nothing can't be grounding-flagged). The PWA renders a calm "from
 * general knowledge — not your notes" chip. Ephemeral — never persisted. */
export interface GeneralKnowledgeEvent {
  type: "general_knowledge";
}
/** A web-sandboxed sub-agent child `jerv` launched inside a `spawn_subagent` fan
 * (docs/archive/SUBAGENT_SPAWNING_PLAN.md). `tool_call_id` anchors the row under the spawning
 * tool call; `child_id` keys it; `persona` is a neutral text tag (never a color).
 * Backend-authored live telemetry — ephemeral, never persisted. */
export interface SubagentSpawnedEvent {
  type: "subagent_spawned";
  tool_call_id: string;
  child_id: string;
  persona: string;
  label: string;
  depth: number;
  /** Which wave of a staged (feeding) fan this child is in (0-based; 0 for a flat fan). */
  wave?: number;
  /** For a wave-2 consumer, the earlier-wave producer labels fed into it ("← fed by …"). */
  fed_from?: string[];
}
/** A status tick for a running child. `phase` is a coarse working word (children run
 * non-streaming in v1). `tree_spent`/`tree_budget` snapshot the shared tree pool for
 * the budget meter. Ephemeral. */
export interface SubagentProgressEvent {
  type: "subagent_progress";
  tool_call_id: string;
  child_id: string;
  phase: string;
  /** The child's ReAct step so far (0 at launch) — drives the live "· N steps" count. */
  step: number;
  tree_spent: number;
  tree_budget: number;
}
/** A running child's live context fill (its loop's per-call usage, forwarded by
 * child_id) so the fan row shows a context meter — the non-streaming twin of the
 * parent turn's UsageEvent. `used` is the latest call's prompt+output; `context_window`
 * is the child model's total window. Ephemeral. */
export interface SubagentUsageEvent {
  type: "subagent_usage";
  tool_call_id: string;
  child_id: string;
  used: number;
  context_window: number;
}
/** A live token slice from a running child: its loop streams turns, and each
 * answer/reasoning chunk is forwarded (tagged by child_id) so the fan shows the child
 * working — a live mini-transcript. Ephemeral. */
export interface SubagentDeltaEvent {
  type: "subagent_delta";
  tool_call_id: string;
  child_id: string;
  channel: "answer" | "reasoning";
  text: string;
}
/** A tool step a running child took — forwarded so the fan's child frame shows its
 * work as a live "Worked" list (`arg` is the query/url it used). Ephemeral. */
export interface SubagentToolEvent {
  type: "subagent_tool";
  tool_call_id: string;
  child_id: string;
  name: string;
  arg: string;
  ok: boolean;
}
/** A child finished: `ok` (clean substantive answer) → green ✓, else → rose ✕.
 * `summary` is its answer or error/truncation note (shown on expand); the budget
 * snapshot refreshes the meter. Ephemeral. */
export interface SubagentDoneEvent {
  type: "subagent_done";
  tool_call_id: string;
  child_id: string;
  ok: boolean;
  stop_reason: string;
  summary: string;
  tree_spent: number;
  tree_budget: number;
}

export type ChatEvent =
  | TextDelta
  | ReasoningDelta
  | ToolCallEvent
  | ToolResultEvent
  | ToolViewEvent
  | ToolProgressEvent
  | JobEnqueuedEvent
  | UsageEvent
  | DoneEvent
  | RunEvent
  | VerdictEvent
  | GeneralKnowledgeEvent
  | SubagentSpawnedEvent
  | SubagentProgressEvent
  | SubagentUsageEvent
  | SubagentDeltaEvent
  | SubagentToolEvent
  | SubagentDoneEvent;

/** A persisted conversation turn (GET /api/sessions/{id}/transcript) — replays a
 * session on reopen. Assistant turns carry their tool steps + note sources. */
export interface TranscriptTurn {
  role: "user" | "assistant";
  content: string;
  tools: {
    id: string;
    name: string;
    ok: boolean | null;
    /** The call's arguments, persisted so an expanded step replays what it ran. */
    args?: Record<string, unknown>;
    /** The verbatim result text, persisted so a step's result rung replays on
     * reopen — the only content a sourceless tool (the web tools) can show. */
    summary?: string;
    sources: NoteSource[];
    /** Web pages a jerv internet tool reached (favicon citation chips), persisted
     * so the chips and their [^n] targets replay on reopen. */
    web_sources?: WebSource[];
    /** A staged proposal / resolved entities the step surfaced, persisted so the
     * bubble's chips and inline links replay on reopen (not just note sources). */
    proposal?: ProposalRef | null;
    entities?: EntityRef[];
    /** A rich tool-result view (e.g. a list_card), persisted so it replays too. */
    view?: ViewPayload | null;
    /** The answer-text length when the tool was called — the split point an image
     * turn replays around (preamble → image → reply). */
    text_offset?: number;
    /** The reasoning-trace length when the tool was called — where it interleaves into
     * the "Thinking" disclosure on reopen (like a sub-agent's trace). */
    reasoning_offset?: number;
  }[];
  /** The assistant turn's reasoning trace (gpt-oss/GLM), for the "thinking"
   * disclosure; "" for user turns and non-reasoning models. */
  reasoning?: string;
  /** Files the owner attached to a user turn, replayed as chips inside the
   * bubble. Present (and possibly empty) on user turns; absent on assistant ones. */
  attachments?: ChatAttachment[];
}

// --- Agent sessions (the capability record; /api/sessions) ---

export interface AgentSession {
  id: string;
  title: string;
  status: string;
  /** The selected agent persona (curator | teacher | jerv); defaults to curator. */
  agent: string;
  domain_scopes: string[];
  subject_ids: string[];
  created_at: string;
  last_active_at: string;
  /** Chats-card metadata (server fills these on the list; absent elsewhere). */
  turn_count?: number;
  preview?: string;
  staged_count?: number;
  /** Sub-agent nesting (docs/archive/SUBAGENT_SPAWNING_PLAN.md Wave S4): a child carries its
   * parent's id (nested under it, excluded from top-level bucketing); a parent carries
   * how many direct children it spawned (the rail count). */
  parent_session_id?: string | null;
  subagent_count?: number;
  /** The latest run's status (running | done | error) — the nested rail shows a
   * child's settled outcome (error → rose ✕) and the parent's failed roll-up. */
  last_run_status?: string | null;
  /** The last completed turn's context fill + the window it ran against, so the
   * composer's context-usage meter restores when this chat is reopened (null/absent
   * until a turn reports usage). */
  context_tokens?: number | null;
  context_window?: number | null;
}

export interface SessionCreate {
  domain_scopes: string[];
  subject_ids?: string[];
  title?: string;
  /** The selected agent persona; omitted defaults to curator on the server. */
  agent?: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

/** A file the owner attached to a chat turn (POST /api/sessions/{id}/attachments).
 * Rendered as a compact chip in the composer and inside the user bubble. */
export interface ChatAttachment {
  id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
}

/** A calendar → Full Brain handoff's appointment: the title the owner sees on
 * the composer pill, the id the agent resolves (read_appointment). */
export interface AppointmentRef {
  id: string;
  title: string;
}

export interface ChatRequest {
  session_id: string;
  message: string;
  history?: ChatMessage[];
  /** An appointment the owner is asking about (a calendar handoff). The id lets
   * the agent resolve the exact appointment; it never enters the transcript. */
  appointment_id?: string;
  /** The PWA's live position for this turn — the same warm geolocation fix note
   * sends attach (only when capture is on). Lets the location tool answer from the
   * phone's current spot; turn-local, never persisted. */
  latitude?: number;
  longitude?: number;
  /** Ids of files the owner attached this turn (uploaded ahead of the send). The
   * agent reads their extracts/vision; the bubble shows them as chips. */
  attachment_ids?: string[];
  /** The owner's per-conversation agent-model pick (the omnibox long-press sheet):
   * a LOCAL catalog id the turn's agent.turn runs on instead of the resolved default.
   * Turn-local — never persisted on the session; the backend validates it against the
   * catalog and ignores an unknown id (the turn runs on the default). */
  model?: string;
  /** This turn carries a Proposal ENACT OUTCOME the owner just produced inline (not
   * owner prose): `message` is the server-authored summary, framed as a data report so
   * the assistant acknowledges and continues without re-staging declined items. */
  proposal_outcome?: boolean;
}

// --- Proposals (the review inbox; /api/proposals) ---

export type ProposalKind =
  | "correction"
  | "knowledge"
  | "merge"
  | "appointment"
  | "wiki-restructure"
  | "egress"
  // Guided intake (W6): an editable mint-a-link draft, and a captured submission
  // materialized into a single note for review.
  | "intake-link"
  | "intake-submission";

export interface ProposalSummary {
  id: string;
  kind: ProposalKind;
  status: string;
  domain: string;
  title: string;
  node_count: number;
}

export interface ProposalNode {
  id: string;
  parent_id: string | null;
  type: "group" | "leaf";
  op: string;
  label: string;
  preview: Record<string, unknown>;
  deps: string[];
  status: string;
}

export interface ProposalDetail {
  id: string;
  kind: ProposalKind;
  status: string;
  domain: string;
  title: string;
  nodes: ProposalNode[];
}

export type Decision = "approve" | "reject";

export interface EnactResult {
  enacted: string[];
  held: string[];
  /** A server-authored, DB-derived summary of what the enact did (approved /
   * corrected / declined-with-reason / held). The PWA sends it back to the assistant
   * as a data-framed turn so it follows up. "" when nothing ran. */
  outcome: string;
}
