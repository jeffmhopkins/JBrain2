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
export interface ToolCallEvent {
  type: "tool_call";
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}
export interface ToolResultEvent {
  type: "tool_result";
  tool_call_id: string;
  ok: boolean;
  summary: string;
}
export interface ToolViewEvent {
  type: "tool_view";
  tool_call_id: string;
  view: ViewPayload;
}
export interface JobEnqueuedEvent {
  type: "job_enqueued";
  job_id: string;
  summary: string;
}
export interface DoneEvent {
  type: "done";
  stop_reason: string;
}

export type ChatEvent =
  | TextDelta
  | ToolCallEvent
  | ToolResultEvent
  | ToolViewEvent
  | JobEnqueuedEvent
  | DoneEvent;

// --- Agent sessions (the capability record; /api/sessions) ---

export interface AgentSession {
  id: string;
  title: string;
  status: string;
  domain_scopes: string[];
  subject_ids: string[];
  created_at: string;
  last_active_at: string;
}

export interface SessionCreate {
  domain_scopes: string[];
  subject_ids?: string[];
  title?: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ChatRequest {
  session_id: string;
  message: string;
  history?: ChatMessage[];
}
