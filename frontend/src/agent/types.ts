// The agent chat wire shapes, mirroring backend agent/contracts.py. These cross
// the SSE wire from /api/chat (ChatEvent) and carry the tool-result views the
// component registry renders (ViewPayload / CitationRef). Hand-written until
// OpenAPI generation lands (docs/DEVELOPMENT.md "TypeScript").

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
  | GeneralKnowledgeEvent;

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
    /** A staged proposal / resolved entities the step surfaced, persisted so the
     * bubble's chips and inline links replay on reopen (not just note sources). */
    proposal?: ProposalRef | null;
    entities?: EntityRef[];
    /** A rich tool-result view (e.g. a list_card), persisted so it replays too. */
    view?: ViewPayload | null;
    /** The answer-text length when the tool was called — the split point an image
     * turn replays around (preamble → image → reply). */
    text_offset?: number;
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
}

// --- Proposals (the review inbox; /api/proposals) ---

export type ProposalKind =
  | "correction"
  | "knowledge"
  | "merge"
  | "appointment"
  | "wiki-restructure"
  | "prompt-edit"
  | "skill-promotion"
  | "egress";

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
}
