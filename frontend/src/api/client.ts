// Single fetch wrapper for the backend API. Auth is a httpOnly session
// cookie, so every request sends credentials and a 401 anywhere means the
// session is gone — the app-level handler flips back to the login screen.
// Types are hand-written until Phase 1 introduces OpenAPI-generated clients
// (docs/DEVELOPMENT.md, "Code standards / TypeScript").
//
// `npm run dev:mock` (VITE_MOCK=1) swaps the transport for in-memory
// fixtures so UI work never needs a backend (docs/DESIGN.md, "UI
// development process"). The flag is a build-time constant and the mock
// module loads via dynamic import, so fixtures never ship in real builds.

import { parseChatStream } from "../agent/chat";
import type {
  AgentSession,
  ChatEvent,
  ChatRequest,
  Decision,
  EnactResult,
  ProposalDetail,
  ProposalSummary,
  SessionCreate,
} from "../agent/types";

export interface Principal {
  principal_id: string;
  kind: string;
  label: string;
}

export interface ContainerStatus {
  service: string;
  state: string;
  health: string | null;
  started_at: string | null;
  image: string;
}

export interface OpsStatus {
  containers: ContainerStatus[];
}

export interface OpsMetrics {
  mem_total_bytes: number;
  mem_available_bytes: number;
  swap_total_bytes: number;
  swap_free_bytes: number;
  disk_total_bytes: number;
  disk_free_bytes: number;
  load_1m: number;
  load_5m: number;
  load_15m: number;
  uptime_seconds: number;
  containers: { service: string; mem_bytes: number }[];
  db: {
    db_size_bytes: number;
    note_count: number;
    attachment_count: number;
    attachment_bytes: number;
  } | null;
  blobs: { file_count: number; total_bytes: number } | null;
}

export interface UpdateStatus {
  state: "none" | "running" | "exited";
  exit_code: number | null;
  log_tail: string;
}

export interface ExportStatus extends UpdateStatus {
  /** Set only once the export one-shot has exited cleanly. */
  filename: string | null;
}

export interface AttachmentOut {
  id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  /** True once the vision pipeline cached OCR/caption text for this file. */
  has_extracts: boolean;
  /** True once a non-empty description is cached (full image analysis). */
  has_description: boolean;
}

/** One vision-cache row, served lazily for the manifest expansion. */
export interface AttachmentExtract {
  kind: string;
  text: string;
  tool: string;
  confidence: number | null;
  created_at: string;
}

export type ImageAnalysisMode = "full" | "ocr";

/** Server-synced user settings (extensible object; image analysis first). */
export interface AppSettings {
  image_analysis_mode: ImageAnalysisMode;
}

export interface NoteOut {
  id: string;
  client_id: string;
  domain: string;
  destination: string | null;
  body: string;
  created_at: string;
  tz_offset_minutes: number | null;
  ingest_state: string;
  /** True once the analyze_note job has written the note_analysis row. */
  analyzed: boolean;
  /** Hidden from the home stream (still searchable); toggled via hide/unhide. */
  hidden: boolean;
  attachments: AttachmentOut[];
  // Owner-eyes capture location (Phase 7 scoped tokens never receive these).
  latitude: number | null;
  longitude: number | null;
  accuracy_m: number | null;
}

export interface NotesPage {
  notes: NoteOut[];
  next_cursor: string | null;
}

export interface NoteCreate {
  client_id: string;
  domain: string;
  destination?: string | null;
  body: string;
  /** Capture instant (ISO 8601). Sent so the offline outbox keeps true time. */
  created_at?: string;
  /** Capture-time UTC offset in minutes east of UTC (negated getTimezoneOffset). */
  tz_offset_minutes?: number;
  latitude?: number;
  longitude?: number;
  accuracy_m?: number;
}

export interface NoteUpdate {
  body?: string;
  domain?: string;
  /** Explicit null clears the destination; an absent key leaves it. */
  destination?: string | null;
}

// ===== Phase 3: analysis, entities, review, LLM usage (docs/ANALYSIS.md) =====

export type FactKind =
  | "event"
  | "measurement"
  | "state"
  | "attribute"
  | "preference"
  | "relationship";

export type FactStatus = "active" | "superseded" | "pending_review" | "retracted";

export interface FactOut {
  id: string;
  entity_id: string;
  entity_name: string;
  predicate: string;
  qualifier: string | null;
  kind: FactKind;
  statement: string;
  value_json: unknown;
  assertion: string;
  status: FactStatus;
  pinned: boolean;
  confidence: number;
  valid_from: string | null;
  valid_to: string | null;
  reported_at: string;
  temporal_precision: string;
  /** May carry literal <mark> around the source words, like search snippets. */
  source_snippet: string | null;
}

export interface AnalysisEntity {
  id: string;
  kind: string;
  name: string;
  status: string;
}

export interface TemporalTokenOut {
  id: string;
  surface_phrase: string;
  kind: string;
  resolved_start: string | null;
  resolved_end: string | null;
  temporal_precision: string;
}

export interface NoteAnalysis {
  note_id: string;
  title: string | null;
  tags: string[];
  /** null = the extraction pass hasn't run yet. */
  analyzed_at: string | null;
  extractor: string | null;
  facts: FactOut[];
  entities: AnalysisEntity[];
  temporal_tokens: TemporalTokenOut[];
}

export interface EntityPredicate {
  predicate: string;
  qualifier: string | null;
  current: FactOut | null;
  /** Full supersession chain, newest first (includes the current fact). */
  history: FactOut[];
}

export interface InboundEdge {
  entity_id: string;
  name: string;
  predicate: string;
  statement: string;
}

export interface EntityMention {
  note_id: string;
  snippet: string;
  created_at: string;
}

export interface EntityOut {
  id: string;
  kind: string;
  canonical_name: string;
  status: string;
  aliases: string[];
  domain: string;
  predicates: EntityPredicate[];
  inbound: InboundEdge[];
  mentions: EntityMention[];
}

export interface EntityListItem {
  id: string;
  kind: string;
  canonical_name: string;
  status: string;
  /** Live edges only: active + pending-review facts with this subject. */
  fact_count: number;
  mention_count: number;
  /** Newest reported_at across the entity's facts; null = mentions only. */
  last_seen: string | null;
}

export interface EntityList {
  items: EntityListItem[];
}

export type ReviewKind =
  | "fact_conflict"
  | "attribute_collision"
  | "merge_proposal"
  | "ambiguous_mention"
  | "domain_promotion"
  | "low_confidence"
  | "split_proposal";

export type ReviewStatus = "open" | "resolved" | "dismissed";

export interface ReviewResolution {
  action: string;
  payload: Record<string, unknown>;
  /** Recorded graph side effects — what reopen reverses; shape is per-kind. */
  effects?: Record<string, unknown>[];
  /** Present = the item was reopened after this decision (tombstone). */
  reopened_at?: string;
}

export interface ReviewItem {
  id: string;
  kind: ReviewKind;
  /** Free-form per-kind payload; the review screen reads it defensively. */
  payload: Record<string, unknown>;
  status: ReviewStatus;
  resolution: ReviewResolution | null;
  domain: string;
  created_at: string;
  resolved_at: string | null;
}

export interface ReviewQueue {
  items: ReviewItem[];
}

export interface ReviewReopened extends ReviewItem {
  /** Set when a permanent effect (distinct_from) survived the unwind. */
  reopen_note: string | null;
}

export interface UsageTotals {
  input_tokens: number;
  output_tokens: number;
  /** null = model missing from the price table; tokens only, never a guess. */
  cost_usd: number | null;
}

export interface TaskUsage extends UsageTotals {
  task: string;
}

export interface DayUsage extends UsageTotals {
  date: string;
}

export interface LlmUsage {
  today: UsageTotals;
  month: UsageTotals;
  by_task: TaskUsage[];
  days: DayUsage[];
}

export type SearchMatch = "semantic" | "keyword" | "both";

export interface SearchResult {
  note_id: string;
  chunk_id: string;
  snippet: string;
  match: SearchMatch;
  score: number;
  domain: string;
  destination: string | null;
  created_at: string;
  body_preview: string;
  attachment_count: number;
  source_kind: string;
  source_anchor: string | null;
}

export interface SearchOut {
  degraded: boolean;
  results: SearchResult[];
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

type UnauthorizedHandler = () => void;

let unauthorizedHandler: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

export const MOCK_MODE = import.meta.env.VITE_MOCK === "1";

// Resolve `fetch` at call time so test stubs (vi.stubGlobal) take effect.
const liveFetch: typeof fetch = (input, init) => fetch(input, init);

let transportPromise: Promise<typeof fetch> | null = null;
function getTransport(): Promise<typeof fetch> {
  transportPromise ??= MOCK_MODE
    ? import("./mock").then((m) => m.mockFetch)
    : Promise.resolve(liveFetch);
  return transportPromise;
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const transport = await getTransport();
  const response = await transport(path, { credentials: "same-origin", ...init });
  if (response.status === 401) {
    unauthorizedHandler?.();
    throw new ApiError(401, "Not authenticated");
  }
  if (!response.ok) {
    throw new ApiError(response.status, `Request failed: ${response.status}`);
  }
  return response;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export function attachmentUrl(id: string): string {
  return `/api/attachments/${encodeURIComponent(id)}`;
}

export function exportFileUrl(name: string): string {
  return `/api/ops/export/file/${encodeURIComponent(name)}`;
}

export const api = {
  async login(ownerKey: string, deviceLabel: string): Promise<void> {
    await request(
      "/api/auth/session",
      jsonInit("POST", { owner_key: ownerKey, device_label: deviceLabel }),
    );
  },

  async me(): Promise<Principal> {
    const response = await request("/api/auth/me");
    return (await response.json()) as Principal;
  },

  async logout(): Promise<void> {
    await request("/api/auth/session", { method: "DELETE" });
  },

  // Idempotent on client_id: retrying after a lost response returns the
  // already-created note instead of duplicating it.
  async createNote(note: NoteCreate): Promise<NoteOut> {
    const response = await request("/api/notes", jsonInit("POST", note));
    return (await response.json()) as NoteOut;
  },

  async getNote(id: string): Promise<NoteOut> {
    const response = await request(`/api/notes/${encodeURIComponent(id)}`);
    return (await response.json()) as NoteOut;
  },

  async listNotes(limit = 50, before?: string): Promise<NotesPage> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (before) params.set("before", before);
    const response = await request(`/api/notes?${params.toString()}`);
    return (await response.json()) as NotesPage;
  },

  // PATCH resets ingest_state to "pending" server-side — the stream's
  // indexing chip reappears until the worker re-chunks the note.
  async updateNote(id: string, patch: NoteUpdate): Promise<NoteOut> {
    const response = await request(
      `/api/notes/${encodeURIComponent(id)}`,
      jsonInit("PATCH", patch),
    );
    return (await response.json()) as NoteOut;
  },

  async deleteNote(id: string): Promise<void> {
    await request(`/api/notes/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // Hide/unhide only flip stream visibility — no re-ingest, so the note keeps
  // its place in Search and stays openable from there.
  async hideNote(id: string): Promise<void> {
    await request(`/api/notes/${encodeURIComponent(id)}/hide`, { method: "POST" });
  },

  async unhideNote(id: string): Promise<void> {
    await request(`/api/notes/${encodeURIComponent(id)}/unhide`, { method: "POST" });
  },

  async search(q: string, domain?: string, limit = 20): Promise<SearchOut> {
    const params = new URLSearchParams({ q, limit: String(limit) });
    if (domain) params.set("domain", domain);
    const response = await request(`/api/search?${params.toString()}`);
    return (await response.json()) as SearchOut;
  },

  async uploadAttachment(noteId: string, blob: Blob, filename: string): Promise<AttachmentOut> {
    const form = new FormData();
    form.append("file", blob, filename);
    const response = await request(`/api/notes/${encodeURIComponent(noteId)}/attachments`, {
      method: "POST",
      body: form,
    });
    return (await response.json()) as AttachmentOut;
  },

  async deleteAttachment(id: string): Promise<void> {
    await request(`/api/attachments/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // Fetched on first expand of a manifest row — never inlined into notes.
  async attachmentExtracts(id: string): Promise<AttachmentExtract[]> {
    const response = await request(`/api/attachments/${encodeURIComponent(id)}/extracts`);
    return ((await response.json()) as { extracts: AttachmentExtract[] }).extracts;
  },

  // On-demand full analysis for one attachment regardless of the global
  // image-analysis mode (also the re-run path); 409 while one is in flight.
  async analyzeAttachment(id: string): Promise<void> {
    await request(`/api/attachments/${encodeURIComponent(id)}/analyze`, { method: "POST" });
  },

  // The first server-synced settings (theme/text-size stay device-local).
  async getSettings(): Promise<AppSettings> {
    const response = await request("/api/settings");
    return (await response.json()) as AppSettings;
  },

  async updateSettings(patch: Partial<AppSettings>): Promise<AppSettings> {
    const response = await request("/api/settings", jsonInit("PUT", patch));
    return (await response.json()) as AppSettings;
  },

  async noteAnalysis(noteId: string): Promise<NoteAnalysis> {
    const response = await request(`/api/notes/${encodeURIComponent(noteId)}/analysis`);
    return (await response.json()) as NoteAnalysis;
  },

  // Note-level re-run: queues a fresh analysis pass (202 with a job id);
  // 409 while one is already in flight — the UI reads both as "running".
  async analyzeNote(id: string): Promise<void> {
    await request(`/api/notes/${encodeURIComponent(id)}/analyze`, { method: "POST" });
  },

  // Non-merged entities, newest-seen first (server-capped at 200).
  async listEntities(q?: string, kind?: string): Promise<EntityList> {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (kind) params.set("kind", kind);
    const qs = params.toString();
    const response = await request(`/api/entities${qs ? `?${qs}` : ""}`);
    return (await response.json()) as EntityList;
  },

  async getEntity(entityId: string): Promise<EntityOut> {
    const response = await request(`/api/entities/${encodeURIComponent(entityId)}`);
    return (await response.json()) as EntityOut;
  },

  // "resolved" is the full decision log: it folds in dismissals and
  // reopened tombstones, newest decision first.
  async reviewQueue(status: "open" | "resolved" = "open"): Promise<ReviewQueue> {
    const response = await request(`/api/review?status=${status}`);
    return (await response.json()) as ReviewQueue;
  },

  // Skip is client-side only (cycle to the back of the local queue) — there
  // is deliberately no skip action on the wire.
  async reviewResolve(
    id: string,
    action: string,
    payload: Record<string, unknown> = {},
  ): Promise<ReviewItem> {
    const response = await request(
      `/api/review/${encodeURIComponent(id)}/resolve`,
      jsonInit("POST", { action, payload }),
    );
    return (await response.json()) as ReviewItem;
  },

  // Full unwind: the backend reverses the resolution's recorded graph
  // effects and re-queues the item; 409 when it is already open.
  async reviewReopen(id: string): Promise<ReviewReopened> {
    const response = await request(`/api/review/${encodeURIComponent(id)}/reopen`, {
      method: "POST",
    });
    return (await response.json()) as ReviewReopened;
  },

  async llmUsage(): Promise<LlmUsage> {
    const response = await request("/api/ops/llm-usage");
    return (await response.json()) as LlmUsage;
  },

  async opsMetrics(): Promise<OpsMetrics> {
    const response = await request("/api/ops/metrics");
    return (await response.json()) as OpsMetrics;
  },

  async opsUpdateStart(): Promise<{ updater: string }> {
    const response = await request("/api/ops/update", { method: "POST" });
    return (await response.json()) as { updater: string };
  },

  async opsUpdateStatus(): Promise<UpdateStatus> {
    const response = await request("/api/ops/update/status");
    return (await response.json()) as UpdateStatus;
  },

  async opsExportStart(): Promise<void> {
    await request("/api/ops/export", { method: "POST" });
  },

  async opsExportStatus(): Promise<ExportStatus> {
    const response = await request("/api/ops/export/status");
    return (await response.json()) as ExportStatus;
  },

  async opsImportUpload(file: File): Promise<{ archive: string }> {
    const form = new FormData();
    form.append("file", file, file.name);
    const response = await request("/api/ops/import/upload", { method: "POST", body: form });
    return (await response.json()) as { archive: string };
  },

  async opsImportStart(archive: string): Promise<void> {
    await request("/api/ops/import/start", jsonInit("POST", { archive }));
  },

  async opsImportStatus(): Promise<UpdateStatus> {
    const response = await request("/api/ops/import/status");
    return (await response.json()) as UpdateStatus;
  },

  async opsResetStart(): Promise<void> {
    await request("/api/ops/reset", { method: "POST" });
  },

  async opsResetStatus(): Promise<UpdateStatus> {
    const response = await request("/api/ops/reset/status");
    return (await response.json()) as UpdateStatus;
  },

  async opsStatus(): Promise<OpsStatus> {
    const response = await request("/api/ops/status");
    return (await response.json()) as OpsStatus;
  },

  async opsRestart(service: string): Promise<void> {
    await request("/api/ops/restart", jsonInit("POST", { service }));
  },

  async opsLogs(service: string, tail: number): Promise<string> {
    const response = await request(`/api/ops/logs/${encodeURIComponent(service)}?tail=${tail}`);
    return await response.text();
  },

  // EventSource cannot surface a 401, so a dead stream only shows as a
  // connection error in the viewer rather than forcing logout. In mock mode
  // the stream simply errors — log-following is out of mock scope.
  opsLogStream(service: string): EventSource {
    return new EventSource(`/api/ops/logs/${encodeURIComponent(service)}/stream`);
  },

  // ===== Phase 4: the agent — sessions + Full Brain chat (docs/ASSISTANT.md) =====

  async listSessions(): Promise<AgentSession[]> {
    const response = await request("/api/sessions");
    return (await response.json()) as AgentSession[];
  },

  async createSession(body: SessionCreate): Promise<AgentSession> {
    const response = await request("/api/sessions", jsonInit("POST", body));
    return (await response.json()) as AgentSession;
  },

  // POST /api/chat streams the agent turn as SSE; the body is a ReadableStream
  // (EventSource is GET-only and can't carry a request body). Yields each parsed
  // ChatEvent so the caller renders text/tool activity live.
  async *chat(body: ChatRequest): AsyncGenerator<ChatEvent> {
    const response = await request("/api/chat", jsonInit("POST", body));
    if (!response.body) return;
    yield* parseChatStream(response.body);
  },

  async listProposals(): Promise<ProposalSummary[]> {
    const response = await request("/api/proposals");
    return (await response.json()) as ProposalSummary[];
  },

  async getProposal(id: string): Promise<ProposalDetail> {
    const response = await request(`/api/proposals/${encodeURIComponent(id)}`);
    return (await response.json()) as ProposalDetail;
  },

  async decideNode(proposalId: string, nodeId: string, decision: Decision): Promise<void> {
    await request(
      `/api/proposals/${encodeURIComponent(proposalId)}/nodes/${encodeURIComponent(nodeId)}/decision`,
      jsonInit("POST", { decision }),
    );
  },

  async enactProposal(id: string): Promise<EnactResult> {
    const response = await request(`/api/proposals/${encodeURIComponent(id)}/enact`, {
      method: "POST",
    });
    return (await response.json()) as EnactResult;
  },
};
