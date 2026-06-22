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
  ChatAttachment,
  ChatEvent,
  ChatRequest,
  Decision,
  EnactResult,
  ProposalDetail,
  ProposalSummary,
  SessionCreate,
  TranscriptTurn,
} from "../agent/types";

export interface Principal {
  principal_id: string;
  kind: string;
  label: string;
}

/** A provisioned device with its location activity (GET /api/locations/devices).
 * `id` is the device's subject id; activity fields are null until its first fix. */
export interface DeviceSummary {
  id: string;
  label: string;
  created_at: string;
  revoked: boolean;
  last_seen: string | null;
  battery_pct: number | null;
  connection: string | null;
  /** Speed (m/s) of the device's latest fix; null until it reports one. */
  velocity_mps: number | null;
  fix_count: number;
}

/** A minted pairing code (POST /api/pairing/codes). `payload` is the self-contained
 * string the phone scans/pastes — it embeds the server URL + the code, so the app
 * needs nothing configured. `code` is the human-readable form; `expires_at` its TTL. */
export interface PairingCode {
  code: string;
  expires_at: string;
  payload: string;
}

/** One geofence crossing for the Timeline feed (GET /api/locations/timeline).
 * `subject_id` is the device; `transition` is "enter" | "exit". */
export interface TimelineEntry {
  occurred_at: string;
  subject_id: string;
  transition: string;
  place_entity_id: string;
  place_name: string;
}

/** One stored fix for the map (GET /api/locations/fixes), trimmed to what the
 * map draws — never the raw OwnTracks metadata. */
export interface LocationFix {
  captured_at: string;
  latitude: number;
  longitude: number;
  accuracy_m: number | null;
  battery_pct: number | null;
  /** Speed (m/s) at this fix; null when the device didn't report one. Drives the
   * speed-colored trail. */
  velocity_mps: number | null;
  /** Heading (degrees, 0–360); per-fix telemetry for the tap-to-inspect popup. */
  course_deg: number | null;
  /** Absolute linear acceleration (m/s²), 0.2 s-filtered. */
  acceleration_mps2: number | null;
  /** Altitude (m). */
  altitude_m: number | null;
}

export interface LatLon {
  lat: number;
  lon: number;
}

/** A geofenced place for the map overlay (GET /api/locations/places): a circle
 * (center + radius_m) or a polygon (ring of points). */
export interface PlaceGeofence {
  place_entity_id: string;
  name: string;
  enabled: boolean;
  center: LatLon | null;
  radius_m: number | null;
  polygon: LatLon[] | null;
}

/** One subject a family member may see (GET /api/member/roster): its label, latest
 * activity, and the latest fix's coordinate so the map can pin everyone. `last_seen`
 * / `latitude` / `longitude` are null until the subject's first fix. Scoped to self +
 * family group by RLS. */
export interface MemberSubject {
  subject_id: string;
  label: string;
  last_seen: string | null;
  battery_pct: number | null;
  connection: string | null;
  latitude: number | null;
  longitude: number | null;
  /** Latest speed (m/s), for the "current speed (if moving)" dock readout. */
  velocity_mps: number | null;
  /** True for the viewer's own device — the live map updates it on every fix while
   * others coalesce to a slower cadence. */
  is_self: boolean;
}

/** One bar on a day's place-track in the digest (L7a): a place name (null = a
 * no-signal gap) and the fraction of the local day [0,1] it spans. Names + times
 * only — there is no coordinate anywhere in the digest. */
export interface PlaceSegment {
  place_name: string | null;
  start: number;
  width: number;
  entered_at: string;
  exited_at: string;
}

/** One local civil day as a place-track: its segments, whether the owner was home
 * for any part of it, and whether it carried any signal at all. */
export interface DayTrack {
  day: string;
  segments: PlaceSegment[];
  home: boolean;
  has_data: boolean;
}

export interface PlaceSeen {
  place_name: string;
  first_seen: string;
  last_seen: string;
}

export interface Trip {
  place_name: string;
  day: string;
  entered_at: string;
  exited_at: string;
  seconds: number;
}

/** The owner's place digest (GET /api/locations/digest) — a compute-on-read rollup
 * of recent place activity, names + times only. `period` is "week" (default) or
 * "night". Owner-only; no coordinates. */
export interface LocationDigest {
  period: string;
  since: string;
  until: string;
  timezone: string;
  days: DayTrack[];
  nights_home: number;
  nights_total: number;
  places_visited: number;
  longest_trip: Trip | null;
  seen: PlaceSeen[];
  computed_at: string;
}

/** The owner's own current/last-known presence (GET /api/locations/presence) for
 * the app-open toast. `present` false → no usable fix; `stale` flips it to the amber
 * "last known" tone. Names + times only. */
export interface LocationPresence {
  present: boolean;
  place_name: string | null;
  last_seen: string | null;
  age_seconds: number | null;
  stale: boolean;
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
  /** iGPU/dGPU utilization 0-100, or null when the host exposes no GPU telemetry. */
  gpu_busy_percent: number | null;
  /** Fan speeds in RPM keyed by sensor label, or null when the host exposes no fan telemetry. */
  fan_rpm: Record<string, number> | null;
  containers: { service: string; mem_bytes: number }[];
  db: {
    db_size_bytes: number;
    note_count: number;
    attachment_count: number;
    attachment_bytes: number;
  } | null;
  blobs: { file_count: number; total_bytes: number } | null;
}

/** The history-graph windows the Ops screen offers (mirror of the backend's set). */
export type MetricRange = "6h" | "24h" | "2d" | "7d" | "30d" | "90d" | "1y";

/** One downsampled bucket. mem/swap/disk are *used* bytes alongside their totals;
 * any field is null when the host didn't report it (e.g. no GPU/fans). */
export interface MetricPoint {
  t: string;
  load_1m: number | null;
  load_5m: number | null;
  load_15m: number | null;
  mem_used_bytes: number | null;
  mem_total_bytes: number | null;
  swap_used_bytes: number | null;
  disk_used_bytes: number | null;
  disk_total_bytes: number | null;
  gpu_busy_percent: number | null;
  fan_rpm_max: number | null;
}

export interface MetricsHistory {
  /** "raw" 30s samples for short spans, the "hourly" rollup for long ones. */
  resolution: "raw" | "hourly";
  step_seconds: number;
  since: string;
  until: string;
  points: MetricPoint[];
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
  /** Per-word transcript breakdown ({text, start_ms, end_ms, confidence}), for the
   * karaoke transcript card. Present on transcript rows only; null otherwise. */
  words: { text: string; start_ms: number; end_ms: number; confidence: number }[] | null;
}

export type ImageAnalysisMode = "full" | "ocr";

/** Server-synced user settings (extensible object; image analysis first). */
export interface AppSettings {
  image_analysis_mode: ImageAnalysisMode;
  // The owner's IANA display timezone (e.g. "America/New_York"), or null when
  // unset. Server-rendered times (the agent's appointment prose) localize to it
  // so they match the cards the client localizes to the browser zone.
  owner_timezone: string | null;
}

/** The read-only appointments ICS feed: enabled state + the URL token (owner-only). */
export interface FeedConfig {
  enabled: boolean;
  token: string | null;
}

// ----- Debug-console capability tokens (owner mints; an assistant uses) -----

export interface DebugToken {
  id: string;
  label: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

/** The mint response — `payload` (server URL + key) is shown exactly once. */
export interface DebugTokenMint {
  id: string;
  label: string;
  expires_at: string | null;
  payload: string;
}

// ----- Per-task LLM routing (GET/PUT /api/settings/llm) -----

/**
 * "grok" | "claude" are always present; enabling local hosting adds one id per
 * provisioned catalog model, so the set is open — keep it a string.
 */
export type LlmProviderId = string;
export type ReasoningEffort = "none" | "low" | "medium" | "high";

export interface LlmProvider {
  id: LlmProviderId;
  label: string;
  /** Whether this provider/model honors a reasoning level (grok, or a local
   * reasoning model like gpt-oss/GLM); the UI hides the control otherwise. */
  supports_reasoning: boolean;
  /** Vision tasks only offer vision-capable providers (cloud, or VL local models). */
  supports_vision: boolean;
}

/** One routable task: which provider runs it, and (grok only) how hard it thinks. */
export interface LlmTask {
  id: string;
  label: string;
  provider: LlmProviderId;
  /** null whenever provider !== "grok" — the wire mirrors the UI's disabling. */
  reasoning_effort: ReasoningEffort | null;
}

/** A catalog model for the "Manage local models" drawer (read-only — weights
 * are provisioned server-side, off by default). */
export interface LocalModelInfo {
  id: string;
  label: string;
  enabled: boolean;
  /** Runtime state from the gateway (best-effort): resident in memory right now. */
  loaded: boolean;
  supports_vision: boolean;
  supports_tools: boolean;
  tiers: string[];
  quant: string;
  /** Catalog's nominal download estimate — used for models not installed here. */
  size_gb: number;
  /** Real measured weights size on disk, or null when the model isn't provisioned. */
  disk_gb: number | null;
  note: string;
  /** The model's catalog default context window — the gateway's `-c` absent an
   * override, and the ceiling the size picker caps at. */
  context_window: number;
  /** The operator's per-model override (tokens), or null to use the default. */
  context_window_override: number | null;
  /** Whether the operator has staged this model (the middle lifecycle state). */
  staged: boolean;
  /** Estimated KV-cache GB at the effective window — the context portion of the bar. */
  kv_gb: number;
}

/** Result of an unload: the catalog ids still resident, and whether the gateway answered. */
export interface LoadedLocalModels {
  loaded: string[];
  reachable: boolean;
}

export interface LlmSettings {
  providers: LlmProvider[];
  reasoning_efforts: ReasoningEffort[];
  reasoning_default: ReasoningEffort;
  tasks: LlmTask[];
  local_hosting_enabled: boolean;
  local_models: LocalModelInfo[];
  /** Live unified-memory gauge for the drawer meter; null when hosting is off / off-Linux. */
  host_memory: { total_gb: number; used_gb: number } | null;
}

/** One task's desired routing; reasoning_effort applies only to a reasoning-capable
 * provider/model (grok, or a local gpt-oss/GLM) and is dropped otherwise. */
export interface LlmTaskPatch {
  provider: LlmProviderId;
  reasoning_effort?: ReasoningEffort;
}

/** A partial update keyed by task id — only the touched tasks travel. */
export interface LlmSettingsPatch {
  tasks: Record<string, LlmTaskPatch>;
}

/** One on-box image model for the settings drawer (GET /api/settings/image). */
export interface ImageModelInfo {
  id: string;
  label: string;
  /** "generate" (text→image) or "edit" (image→image). */
  kind: string;
  /** Offered to jerv (in the provisioned set) on this box. */
  enabled: boolean;
  recommended: boolean;
  /** Catalog's nominal download estimate. */
  size_gb: number;
  /** Real measured on-disk size, or null when not provisioned here. */
  disk_gb: number | null;
  /** Resident unified-memory footprint estimate — the RAM-budget reservation. */
  vram_gb: number;
  note: string;
}

/** The ComfyUI image service's state — its catalog models, reachability, and the
 * real VRAM gauge from /system_stats (shares the LLM drawer's unified-memory bar). */
export interface ImageSettings {
  enabled: boolean;
  reachable: boolean;
  models: ImageModelInfo[];
  /** Real VRAM total/free from ComfyUI; null when unreachable or unreported. */
  memory: { total_gb: number; free_gb: number } | null;
}

/** One attendee on an appointment — name plus optional iCalendar params. */
export interface AttendeeOut {
  name: string;
  entity_id: string | null;
  role: string | null;
  status: string | null;
  required: boolean | null;
}

/** One appointment from the projection (read-only; ISO times, status is a flag).
 * `location` is present only when the session can see its (location) domain. */
export interface AppointmentOut {
  id: string;
  title: string;
  domain: string;
  start: string;
  end: string | null;
  all_day: boolean;
  status: string;
  location: string | null;
  organizer: string | null;
  attendance_mode: string | null;
  online_url: string | null;
  description: string | null;
  appointment_type: string | null;
  rrule: string | null;
  recurring: boolean;
  attendees: AttendeeOut[];
  source_note_id: string | null;
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
  /** "human" (owner-captured) or "agent" (enacted from a Proposal). Drives the
   * stream's "assistant" tag; attribution is metadata, never body prose. */
  provenance: string;
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
  /** A relationship fact's object node: the value IS this entity (me.owns →
   * the F-150), so the edge links to it instead of rendering the statement.
   * Null for scalar facts, or when the object isn't visible to the session. */
  object_entity_id: string | null;
  object_entity_name: string | null;
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
  /** The owner-set profile image sha (served by GET /api/entities/{id}/image). */
  image_sha?: string | null;
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

// ----- Graph view (the ego subgraph; GET /api/entities/{id}/neighbors) -----

/** One node in the ego subgraph; mirrors the browse-list shape, no facts. */
export interface GraphNode {
  id: string;
  kind: string;
  canonical_name: string;
  status: string;
  domain: string;
}

/** A directed relationship edge source --predicate--> target. */
export interface GraphEdge {
  source: string;
  target: string;
  predicate: string;
}

export interface EgoGraph {
  /** The entity the view centers on; "" when the whole graph has no "Me". */
  root: string;
  /** Hops traversed (ego: 1–2); 0 marks the whole-graph default. */
  depth: number;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

// ===== Lists (the owner's structured records; /api/lists) =====

export interface ListItemOut {
  id: string;
  body: string;
  checked: boolean;
}

export interface ListOut {
  id: string;
  title: string;
  domain: string;
  archived: boolean;
  items: ListItemOut[];
}

export type ReviewKind =
  | "fact_conflict"
  | "attribute_collision"
  | "merge_proposal"
  | "ambiguous_mention"
  | "domain_promotion"
  | "low_confidence"
  | "low_confidence_inference"
  | "split_proposal"
  | "extraction_truncated"
  | "new_predicate"
  | "confirm_entity";

export type ReviewStatus = "open" | "resolved" | "dismissed" | "deferred";

// The inbox's two lanes map onto wire status filters: pending=open,
// decided=resolved (the decided list folds in dismissals and reopened
// tombstones). The "deferred" wire status is retained for legacy rows; the UI
// no longer parks items, so it has no lane.
export type ReviewFilter = "pending" | "decided";
export const FILTER_STATUS: Record<ReviewFilter, "open" | "resolved"> = {
  pending: "open",
  decided: "resolved",
};

export interface BatchDecision {
  id: string;
  action: string;
  payload?: Record<string, unknown>;
}

export interface ResolveBatchResult {
  items: ReviewItem[];
  errors: { id: string; detail: string }[];
}

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

// ===== Phase 5: the workflow run log (the Ops "Runs" surface; /api/runs) =====
// Mirrors backend/src/jbrain/api/runs.py 1:1 — the run log is owner-only.

/** A run's lifecycle state as stored (migration 0016 CHECK). 'error' is the
 * failed state; the Runs surface renders it as the red "failed" tile/dot. */
export type RunStatus = "running" | "done" | "error";

/** A manual/sweep trigger the owner can fire on demand. The list endpoint is
 * sibling Track B's (`GET /api/ops/triggers`); the Runs surface reads it
 * best-effort so the sweep row lights up once B ships, and stays hidden until
 * then rather than breaking. */
export interface SweepTrigger {
  id: string;
  pipeline: string;
  /** Optional human label/description; falls back to the pipeline name. */
  label?: string | null;
}

/** A row in the run log list (GET /api/runs). */
export interface RunSummary {
  id: string;
  /** agent | integration | pipeline — drives the kind chip. */
  kind: string;
  status: RunStatus;
  /** The pipeline (or its trigger's pipeline) names the run; agent runs read "agent". */
  name: string;
  started_at: string;
  /** null while still running — no honest end yet. */
  duration_ms: number | null;
  step_count: number;
  cost_tokens: number;
  /** The first failing step's name; null unless status is "error". */
  last_error: string | null;
}

/** One node in a run's step tree (the split-panel; GET /api/runs/{id}). */
export interface RunStepView {
  idx: number;
  /** model | tool | job — drives the step-kind chip. */
  kind: string;
  name: string;
  ok: boolean;
  cost_tokens: number;
  /** The executor job this step enqueued, when any. */
  job_id: string | null;
  /** A failing step's error text; null on a successful step. */
  error: string | null;
}

export interface RunDetail {
  id: string;
  kind: string;
  status: RunStatus;
  name: string;
  started_at: string;
  duration_ms: number | null;
  step_count: number;
  cost_tokens: number;
  stop_reason: string | null;
  steps: RunStepView[];
}

// ===== Phase 5: the Automations operator surface (the Ops "Workflow" screen) =====
// Mirrors backend/src/jbrain/api/ops.py's /ops/automations + /ops/actions 1:1 —
// the engine config is owner-only.

/** One ordered step of an automation's pipeline, resolved through the action
 * registry so the card renders the cost-class chip + description. */
export interface AutomationStep {
  action: string;
  cost_class: string;
  description: string;
  /** False when the pipeline names an action the registry doesn't carry (drift). */
  known: boolean;
}

/** A run-log row for one automation's recent runs (the expanded card). */
export interface AutomationRun {
  id: string;
  status: RunStatus;
  started_at: string;
  duration_ms: number | null;
  /** A failed run's first-error hint; null unless status is "error". */
  last_error: string | null;
}

/** One "when X -> run Y" card. `kind` is on_event (auto, not manually fireable)
 * or schedule; `group` buckets it into the mock's sections. */
export interface Automation {
  trigger_id: string;
  kind: "on_event" | "schedule";
  group: "event" | "reconcile" | "nightly";
  pipeline: string;
  enabled: boolean;
  /** Manually fireable (a sweep/reconciler). Event triggers are never manual. */
  manual: boolean;
  steps: AutomationStep[];
  recent_runs: AutomationRun[];
  /** Event-bound: the event type that fires it. null for schedules. */
  on_event: string | null;
  /** Schedule-bound: the schedule to toggle alongside the trigger. null for events. */
  schedule_id: string | null;
  interval_seconds: number | null;
  next_run_at: string | null;
  last_run_at: string | null;
}

/** A Catalog row: a registered action's metadata + whether it's seeded in app.actions. */
export interface CatalogAction {
  name: string;
  cost_class: string;
  domain_optional: boolean;
  mutating: boolean;
  description: string;
  seeded: boolean;
}

export interface AutomationsResponse {
  automations: Automation[];
  actions: CatalogAction[];
}

// ===== Phase 6: the wiki — the read-only article reader (docs/mocks/wiki-*) =====
// The wiki is machine-written from notes; the reader renders it current-only and
// read-only, every claim carrying a numbered citation back to its source note.

/** One infobox field: a label and its value, with the citations it carries.
 * `link` flags a wiki→wiki cross-link; `redLink` marks one with no article yet
 * (the muted dotted treatment). Both are presentational hints only here. */
export interface WikiInfoboxField {
  label: string;
  value: string;
  /** Citation numbers this field cites (the [n] superscripts after the value). */
  citations: number[];
  /** Renders the value as a wiki cross-link (steel). */
  link?: boolean;
  /** Renders as a "no article yet" red-link (muted, dotted). */
  redLink?: boolean;
}

/** A Talk-board post (mock B). `author` is one of the three voices; `source` backs the source
 * card and `outcome` the green outcome chip; `rev` is the Build-log "rev N" (else null). */
export interface WikiTalkPost {
  id: string;
  author: "owner" | "editor" | "builder";
  body: string;
  source: { note_id: string; meta: string; snippet: string; domain: string } | null;
  outcome: string | null;
  created_at: string;
  rev: number | null;
}

/** A Talk thread: an owner `discussion` topic (with open/resolved status) or the auto
 * `build_log` topic (with `meta` = "auto · N entries"). */
export interface WikiTalkTopic {
  id: string;
  kind: "discussion" | "build_log";
  title: string;
  status: "open" | "resolved";
  meta: string | null;
  posts: WikiTalkPost[];
}

export interface WikiTalkOut {
  title: string;
  topics: WikiTalkTopic[];
}

/** The infobox: either an entity-type disc OR an owner-added photo slot. */
export interface WikiInfobox {
  title: string;
  /** Entity kind for the type disc (see entities/kinds). Omit when `photo`. */
  kind?: string;
  /** True to render the owner-added photo slot instead of the type disc. */
  photo?: boolean;
  /** Where to load the owner photo from (GET /api/wiki/{id}/image). Set iff `photo`. */
  image_url?: string | null;
  fields: WikiInfoboxField[];
}

/** One run of article prose: plain text, OR text carrying inline [n] markers
 * the renderer turns into citation buttons. The body renderer reads `text`
 * for the [n] markers, so a paragraph is just its raw string. */
export interface WikiParagraph {
  kind: "p";
  /** Prose with inline `[n]` citation markers (e.g. "…in Brookline.[9]"). */
  text: string;
}

export interface WikiList {
  kind: "ul";
  /** Each item is prose with inline `[n]` markers. */
  items: string[];
}

export interface WikiTable {
  kind: "table";
  header: string[];
  /** Each row's cells are prose with inline `[n]` markers. */
  rows: string[][];
}

export type WikiBlock = WikiParagraph | WikiList | WikiTable;

/** A type-guided section (H2): a domain dot + label, prose/list/table blocks,
 * and nested subsections (H3). */
export interface WikiSection {
  heading: string;
  /** Backend domain code (general | health | finance | …) — drives the dot. */
  domain: string;
  blocks: WikiBlock[];
  subsections?: WikiSubsection[];
}

/** A nested subsection (H3) under a section: heading + blocks, no further nest. */
export interface WikiSubsection {
  heading: string;
  blocks: WikiBlock[];
}

/** One numbered reference: the source note's provenance + snippet. `n` is the
 * citation number the [n] superscripts and the References list share. */
export interface WikiReference {
  n: number;
  /** The cited note id, for a future "open the note" jump (unused in B1). */
  note_id: string;
  /** Human provenance line, e.g. "Note · May 2, 2018". */
  meta: string;
  domain: string;
  /** The cited snippet; may carry literal <mark> around the cited words. */
  snippet: string;
}

export interface WikiArticleOut {
  id: string;
  title: string;
  /** The grey one-liner under the title (e.g. "Person · pediatrician · …"). */
  subtitle: string;
  infobox: WikiInfobox;
  /** The opening prose paragraph(s), with inline [n] markers. */
  lead: WikiParagraph[];
  sections: WikiSection[];
  references: WikiReference[];
}

export type SearchMatch = "semantic" | "keyword" | "both";

/** A note/passage hit — the Phase-2 result shape. `kind: "note"` discriminates it
 * from the wiki leg in the merged result list. */
export interface SearchResult {
  kind: "note";
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

/** A wiki-article hit from the search wiki leg (dense over `wiki_index` + FTS over
 * `wiki_revisions.body_tsv`, RLS-scoped server-side). Articles usually out-answer a
 * raw passage, so they rank as the headline result above note hits. */
export interface WikiSearchResult {
  kind: "wiki";
  article_id: string;
  title: string;
  /** The 1–2 sentence `lead_summary` blurb, shown under the title. */
  blurb: string;
  /** Entity kind → the type disc/glyph (people=steel, orgs=violet, …). */
  entity_kind: string;
  /** The matched section's domain — drives the domain chip. */
  domain: string;
  /** The matched body snippet; may carry literal <mark> around the matched words. */
  snippet: string;
  match: SearchMatch;
  score: number;
}

/** One row in the merged result list: a note passage or a wiki article. */
export type SearchHit = SearchResult | WikiSearchResult;

export interface SearchOut {
  degraded: boolean;
  results: SearchHit[];
}

// ----- The wiki landing (Phase 6, Wave B2a — docs/mocks/wiki-landing-a-*.html) -----

/** A landing entry: an article reference rendered as a row/card. */
export interface WikiLandingEntry {
  id: string;
  title: string;
  /** Entity kind → the type disc/glyph + accent. */
  kind: string;
  /** The matched section/article domain (drives nothing visible yet; kept for parity). */
  domain: string;
  /** The per-article `lead_summary` blurb (≤2 lines, clamped in CSS). */
  blurb: string;
}

/** "Recently updated" carries a human "when" line from the last build. */
export interface WikiRecentEntry extends WikiLandingEntry {
  when: string;
}

/** "Most connected" carries the inbound link count (computed post-RLS). */
export interface WikiHubEntry extends WikiLandingEntry {
  links: number;
}

/** "Browse by type": the canonical type label + its A–Z entries. */
export interface WikiTypeGroup {
  type: string;
  entries: WikiLandingEntry[];
}

/** The wiki landing payload: search-first rails over the article set. */
export interface WikiLandingOut {
  recent: WikiRecentEntry[];
  hubs: WikiHubEntry[];
  groups: WikiTypeGroup[];
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

/** Download URL for a chat attachment (distinct from a note attachment's path). */
export function chatAttachmentUrl(id: string): string {
  return `/api/chat-attachments/${encodeURIComponent(id)}`;
}

export function exportFileUrl(name: string): string {
  return `/api/ops/export/file/${encodeURIComponent(name)}`;
}

// Offline stand-ins for the generated-image bytes: an `<img src>` never flows
// through `request()`/`mockFetch`, so in mock dev the by-id URLs would 404. The
// url helpers below swap in these inline-SVG data: URIs when MOCK_MODE is on,
// keyed by the same ids the mock fixture serves — never used in real builds.
// (These hex are placeholder image *content*, not theme styling.)
const svgDataUri = (svg: string): string => `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;

const MOCK_GENIMG: Record<string, string> = {
  "mock-genimg-lighthouse": svgDataUri(
    `<svg xmlns='http://www.w3.org/2000/svg' width='768' height='1024'>
      <defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'>
        <stop offset='0' stop-color='#7a6a9a'/><stop offset='.55' stop-color='#c98a8f'/>
        <stop offset='1' stop-color='#e9c79a'/></linearGradient></defs>
      <rect width='768' height='1024' fill='url(#g)'/>
      <rect x='600' y='760' width='40' height='180' rx='6' fill='#f4ead2'/>
      <circle cx='620' cy='745' r='26' fill='#ffe9b0'/>
      <circle cx='200' cy='180' r='90' fill='#ffe9b0' opacity='.55'/></svg>`,
  ),
  "mock-genimg-lighthouse-stormy": svgDataUri(
    `<svg xmlns='http://www.w3.org/2000/svg' width='768' height='1024'>
      <defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'>
        <stop offset='0' stop-color='#2b3242'/><stop offset='.6' stop-color='#3a4a5e'/>
        <stop offset='1' stop-color='#5a4b66'/></linearGradient></defs>
      <rect width='768' height='1024' fill='url(#g)'/>
      <polygon points='620,745 540,1024 700,1024' fill='#ffe9b0' opacity='.5'/>
      <rect x='600' y='760' width='40' height='180' rx='6' fill='#dcd2c0'/>
      <circle cx='620' cy='745' r='26' fill='#ffe9b0'/></svg>`,
  ),
};

/** Source URL for the served result of a generated image (data-only contract:
 * the tool-view payload carries only `image_id`; the component builds this). */
export function generatedImageUrl(id: string): string {
  if (MOCK_MODE && MOCK_GENIMG[id]) return MOCK_GENIMG[id];
  return `/api/images/generated/${encodeURIComponent(id)}`;
}

/** Source ("before") URL for an edit: the original bytes the edit started from,
 * resolved by the same id on the backend. */
export function generatedImageSourceUrl(id: string): string {
  if (MOCK_MODE && MOCK_GENIMG["mock-genimg-lighthouse"] && id === "mock-genimg-lighthouse-stormy")
    return MOCK_GENIMG["mock-genimg-lighthouse"];
  return `/api/images/generated/${encodeURIComponent(id)}/source`;
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

  /** Stop the in-flight image render (the chat "Stop render" control). Best-effort:
   * a 409 (hosting off) / 502 (gateway) just means the render runs to completion. */
  async interruptImageRender(): Promise<void> {
    await request("/api/settings/image/interrupt", { method: "POST" });
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

  // Per-task LLM routing: the provider each task runs on, plus grok's reasoning
  // level. Grouping into tiers is a frontend concern (the wire is a flat list).
  async getLlmSettings(): Promise<LlmSettings> {
    const response = await request("/api/settings/llm");
    return (await response.json()) as LlmSettings;
  },

  async updateLlmSettings(patch: LlmSettingsPatch): Promise<LlmSettings> {
    const response = await request("/api/settings/llm", jsonInit("PUT", patch));
    return (await response.json()) as LlmSettings;
  },

  /** Evict one local model from the gateway's memory; returns what's still resident. */
  async unloadLocalModel(id: string): Promise<LoadedLocalModels> {
    const response = await request(
      `/api/settings/llm/local-models/${encodeURIComponent(id)}/unload`,
      { method: "POST" },
    );
    return (await response.json()) as LoadedLocalModels;
  },

  /** Warm one local model into memory; returns what's resident after. */
  async loadLocalModel(id: string): Promise<LoadedLocalModels> {
    const response = await request(
      `/api/settings/llm/local-models/${encodeURIComponent(id)}/load`,
      { method: "POST" },
    );
    return (await response.json()) as LoadedLocalModels;
  },

  /** Set (or clear, with null) one model's context window; returns the full snapshot. */
  async setLocalContextWindow(id: string, window: number | null): Promise<LlmSettings> {
    const response = await request(
      `/api/settings/llm/local-models/${encodeURIComponent(id)}/context-window`,
      jsonInit("PUT", { context_window: window }),
    );
    return (await response.json()) as LlmSettings;
  },

  /** Stage / unstage one model (intent to keep it served); returns the full snapshot. */
  async stageLocalModel(id: string, on: boolean): Promise<LlmSettings> {
    const response = await request(
      `/api/settings/llm/local-models/${encodeURIComponent(id)}/stage`,
      { method: on ? "POST" : "DELETE" },
    );
    return (await response.json()) as LlmSettings;
  },

  // ----- On-box image service (ComfyUI), surfaced in the same LLM drawer -----
  async getImageSettings(): Promise<ImageSettings> {
    const response = await request("/api/settings/image");
    return (await response.json()) as ImageSettings;
  },

  /** Unload cached models + free the service's VRAM; returns the refreshed snapshot. */
  async freeImageMemory(): Promise<ImageSettings> {
    const response = await request("/api/settings/image/free", { method: "POST" });
    return (await response.json()) as ImageSettings;
  },

  /** Start the (provisioned) ComfyUI service via the supervisor. */
  async startImageService(): Promise<void> {
    await request("/api/settings/image/service/start", { method: "POST" });
  },

  /** Stop the ComfyUI service via the supervisor (frees its memory by halting it). */
  async stopImageService(): Promise<void> {
    await request("/api/settings/image/service/stop", { method: "POST" });
  },

  // ----- Appointments ICS feed (a revocable, read-only subscribe URL) -----
  async feedConfig(): Promise<FeedConfig> {
    const response = await request("/api/feed/appointments");
    return (await response.json()) as FeedConfig;
  },

  async rotateFeed(): Promise<FeedConfig> {
    const response = await request("/api/feed/appointments/rotate", { method: "POST" });
    return (await response.json()) as FeedConfig;
  },

  async disableFeed(): Promise<void> {
    await request("/api/feed/appointments", { method: "DELETE" });
  },

  // ----- Debug-console capability tokens (owner-only) -----
  async debugTokens(): Promise<DebugToken[]> {
    const response = await request("/api/settings/debug-tokens");
    return (await response.json()) as DebugToken[];
  },

  async mintDebugToken(label: string, ttlHours: number): Promise<DebugTokenMint> {
    const response = await request(
      "/api/settings/debug-tokens",
      jsonInit("POST", { label, ttl_hours: ttlHours }),
    );
    return (await response.json()) as DebugTokenMint;
  },

  async revokeDebugToken(id: string): Promise<void> {
    await request(`/api/settings/debug-tokens/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // The owner's read-only calendar (Day/Week/Month/Tasks read this projection).
  async appointments(): Promise<AppointmentOut[]> {
    const response = await request("/api/appointments");
    return (await response.json()) as AppointmentOut[];
  },

  // One appointment as a single-event .ics blob, fetched (not navigated) so the
  // PWA's service worker can't swap the download for the app shell — the browser
  // hands the blob to the OS calendar.
  async appointmentIcs(id: string): Promise<Blob> {
    const response = await request(`/api/appointments/${encodeURIComponent(id)}.ics`);
    return await response.blob();
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

  /** Upload/replace an entity's owner profile image (multipart). The server sniffs the bytes,
   * stores them content-addressed, and copies the sha onto the entity's wiki article. */
  async uploadEntityImage(
    entityId: string,
    blob: Blob,
    filename: string,
  ): Promise<{ image_sha: string; media_type: string }> {
    const form = new FormData();
    form.append("file", blob, filename);
    const response = await request(`/api/entities/${encodeURIComponent(entityId)}/image`, {
      method: "PUT",
      body: form,
    });
    return (await response.json()) as { image_sha: string; media_type: string };
  },

  // ----- The wiki: a machine-written, read-only encyclopedia -----
  /** The landing rails: recently-updated, most-connected hubs, and the
   * type-grouped index — all derived (no hand-maintained taxonomy). */
  async getWikiLanding(): Promise<WikiLandingOut> {
    const response = await request("/api/wiki/landing");
    return (await response.json()) as WikiLandingOut;
  },

  async getWikiArticle(id: string): Promise<WikiArticleOut> {
    const response = await request(`/api/wiki/${encodeURIComponent(id)}`);
    return (await response.json()) as WikiArticleOut;
  },

  // File an owner correction against an article: an owner-authored note that out-argues the
  // graph (force-supersedes + pins), so the article rebuilds — the wiki stays machine-written.
  async fileCorrection(
    articleId: string,
    correction: { body: string; domain: string; revision_id?: string },
  ): Promise<{ note_id: string; created: boolean }> {
    const response = await request(
      `/api/wiki/${encodeURIComponent(articleId)}/corrections`,
      jsonInit("POST", correction),
    );
    return (await response.json()) as { note_id: string; created: boolean };
  },

  // ----- The wiki Talk board: the article's editorial discussion + auto Build-log -----
  async getTalk(articleId: string): Promise<WikiTalkOut> {
    const response = await request(`/api/wiki/${encodeURIComponent(articleId)}/talk`);
    return (await response.json()) as WikiTalkOut;
  },

  async createTalkTopic(
    articleId: string,
    topic: { title: string; body: string },
  ): Promise<WikiTalkTopic> {
    const response = await request(
      `/api/wiki/${encodeURIComponent(articleId)}/talk/topics`,
      jsonInit("POST", topic),
    );
    return (await response.json()) as WikiTalkTopic;
  },

  async postTalkReply(
    articleId: string,
    topicId: string,
    reply: { body: string },
  ): Promise<WikiTalkPost> {
    const response = await request(
      `/api/wiki/${encodeURIComponent(articleId)}/talk/topics/${encodeURIComponent(topicId)}/posts`,
      jsonInit("POST", reply),
    );
    return (await response.json()) as WikiTalkPost;
  },

  async setTalkTopicStatus(
    articleId: string,
    topicId: string,
    status: "open" | "resolved",
  ): Promise<{ id: string; status: string }> {
    const response = await request(
      `/api/wiki/${encodeURIComponent(articleId)}/talk/topics/${encodeURIComponent(topicId)}`,
      jsonInit("PATCH", { status }),
    );
    return (await response.json()) as { id: string; status: string };
  },

  // Run the Editor (agent) over the topic and get its reply. `afterPostId` is the owner post just
  // filed — the server 409s if it's no longer the latest (the double-submit guard). `post` is null
  // when the Editor produced no prose and pulled no lever.
  async requestEditorReply(
    articleId: string,
    topicId: string,
    afterPostId: string,
  ): Promise<{ post: WikiTalkPost | null }> {
    const response = await request(
      `/api/wiki/${encodeURIComponent(articleId)}/talk/topics/${encodeURIComponent(topicId)}/editor`,
      jsonInit("POST", { after_post_id: afterPostId }),
    );
    return (await response.json()) as { post: WikiTalkPost | null };
  },

  // The ego subgraph for the graph view: the focal entity plus everything
  // within `depth` relationship hops, RLS-scoped server-side.
  async getNeighbors(entityId: string, depth = 2): Promise<EgoGraph> {
    const response = await request(
      `/api/entities/${encodeURIComponent(entityId)}/neighbors?depth=${depth}`,
    );
    return (await response.json()) as EgoGraph;
  },

  // The whole graph for the default view: every visible entity (including
  // disconnected ones) + all relationship edges, `root` = the "Me" entity.
  async getFullGraph(): Promise<EgoGraph> {
    const response = await request("/api/graph");
    return (await response.json()) as EgoGraph;
  },

  // ----- Lists: the owner managing their own data directly -----
  async lists(): Promise<ListOut[]> {
    const response = await request("/api/lists");
    return (await response.json()) as ListOut[];
  },

  async getList(listId: string): Promise<ListOut> {
    const response = await request(`/api/lists/${encodeURIComponent(listId)}`);
    return (await response.json()) as ListOut;
  },

  async createList(title: string, domain: string): Promise<ListOut> {
    const response = await request("/api/lists", jsonInit("POST", { title, domain }));
    return (await response.json()) as ListOut;
  },

  async renameList(listId: string, title: string): Promise<void> {
    await request(`/api/lists/${encodeURIComponent(listId)}`, jsonInit("PATCH", { title }));
  },

  async deleteList(listId: string): Promise<void> {
    await request(`/api/lists/${encodeURIComponent(listId)}`, { method: "DELETE" });
  },

  async addListItem(listId: string, body: string): Promise<ListItemOut> {
    const response = await request(
      `/api/lists/${encodeURIComponent(listId)}/items`,
      jsonInit("POST", { body }),
    );
    return (await response.json()) as ListItemOut;
  },

  async reorderListItems(listId: string, itemIds: string[]): Promise<void> {
    await request(
      `/api/lists/${encodeURIComponent(listId)}/order`,
      jsonInit("PATCH", { item_ids: itemIds }),
    );
  },

  // The checkbox tap on a list_card view and the Lists detail both land here;
  // body renames an item, checked toggles it.
  async setListItemChecked(itemId: string, checked: boolean): Promise<void> {
    await request(`/api/lists/items/${encodeURIComponent(itemId)}`, jsonInit("PATCH", { checked }));
  },

  async renameListItem(itemId: string, body: string): Promise<void> {
    await request(`/api/lists/items/${encodeURIComponent(itemId)}`, jsonInit("PATCH", { body }));
  },

  async removeListItem(itemId: string): Promise<void> {
    await request(`/api/lists/items/${encodeURIComponent(itemId)}`, { method: "DELETE" });
  },

  // "resolved" is the full decision log: it folds in dismissals and
  // reopened tombstones, newest decision first.
  async reviewQueue(status: "open" | "resolved" | "deferred" = "open"): Promise<ReviewQueue> {
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

  // Bulk-apply per-item decisions in one transaction: each carries its own
  // action (the caller knows each row's kind), the good ones commit, the bad
  // ones come back in `errors` so the UI can roll exactly those rows back.
  async reviewResolveBatch(decisions: BatchDecision[]): Promise<ResolveBatchResult> {
    const response = await request("/api/review/resolve-batch", jsonInit("POST", { decisions }));
    return (await response.json()) as ResolveBatchResult;
  },

  // Full unwind: the backend reverses the resolution's recorded graph
  // effects and re-queues the item; 409 when it is already open.
  async reviewReopen(id: string): Promise<ReviewReopened> {
    const response = await request(`/api/review/${encodeURIComponent(id)}/reopen`, {
      method: "POST",
    });
    return (await response.json()) as ReviewReopened;
  },

  // The weighted relation candidates for a held inference's predicate picker,
  // computed on demand (so cards filed before the picker existed get them too).
  async reviewPredicateSuggestions(id: string): Promise<{ name: string; score: number }[]> {
    const response = await request(`/api/review/${encodeURIComponent(id)}/predicate-suggestions`);
    return ((await response.json()) as { suggestions: { name: string; score: number }[] })
      .suggestions;
  },

  async llmUsage(): Promise<LlmUsage> {
    const response = await request("/api/ops/llm-usage");
    return (await response.json()) as LlmUsage;
  },

  async opsMetrics(): Promise<OpsMetrics> {
    const response = await request("/api/ops/metrics");
    return (await response.json()) as OpsMetrics;
  },

  async opsMetricsHistory(range: MetricRange): Promise<MetricsHistory> {
    const response = await request(`/api/ops/metrics/history?range=${range}`);
    return (await response.json()) as MetricsHistory;
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

  // ===== The workflow run log — the Ops "Runs" surface (owner-only) =====

  async runs(): Promise<RunSummary[]> {
    const response = await request("/api/runs");
    return (await response.json()) as RunSummary[];
  },

  async run(id: string): Promise<RunDetail> {
    const response = await request(`/api/runs/${encodeURIComponent(id)}`);
    return (await response.json()) as RunDetail;
  },

  // The manual/sweep triggers for the dashboard's sweep-control row (sibling
  // Track B). Best-effort: the surface treats a missing endpoint as "no sweeps"
  // and simply hides the row, so it never blocks the run log.
  async sweepTriggers(): Promise<SweepTrigger[]> {
    const response = await request("/api/ops/triggers");
    return (await response.json()) as SweepTrigger[];
  },

  // Fire a manual/sweep trigger's pipeline immediately (sibling Track B's
  // endpoint). Idempotent on the server; the new run shows up in `runs()`.
  async runTrigger(triggerId: string): Promise<void> {
    await request(`/api/ops/triggers/${encodeURIComponent(triggerId)}/run`, { method: "POST" });
  },

  // ===== The Automations operator surface (the Ops "Workflow" screen) =====

  // The grouped "when -> do" cards + the action Catalog, from live engine config.
  async automations(): Promise<AutomationsResponse> {
    const response = await request("/api/ops/automations");
    return (await response.json()) as AutomationsResponse;
  },

  // Enable/disable a trigger (the emergency-stop / re-arm). Owner-only mutation.
  async setTriggerEnabled(triggerId: string, enabled: boolean): Promise<void> {
    await request(
      `/api/ops/triggers/${encodeURIComponent(triggerId)}`,
      jsonInit("PATCH", { enabled }),
    );
  },

  // Enable/disable a schedule (stops the tick from firing it). Owner-only mutation.
  async setScheduleEnabled(scheduleId: string, enabled: boolean): Promise<void> {
    await request(
      `/api/ops/schedules/${encodeURIComponent(scheduleId)}`,
      jsonInit("PATCH", { enabled }),
    );
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

  // Stage one file for a chat turn (multipart). The server runs the allowlist
  // (415 on a rejected type) and returns the row the bubble chips against; the
  // id then rides the next /api/chat send as attachment_ids.
  async uploadChatAttachment(sessionId: string, file: File): Promise<ChatAttachment> {
    const form = new FormData();
    form.append("file", file, file.name);
    const response = await request(`/api/sessions/${encodeURIComponent(sessionId)}/attachments`, {
      method: "POST",
      body: form,
    });
    return (await response.json()) as ChatAttachment;
  },

  // Whether the model serving agent.turn can accept images — gates the chat
  // attach affordance (hidden, with a hint, when vision is off).
  async getChatCapabilities(): Promise<{ supports_vision: boolean; can_edit_images: boolean }> {
    const response = await request("/api/chat/capabilities");
    return (await response.json()) as { supports_vision: boolean; can_edit_images: boolean };
  },

  async getTranscript(sessionId: string): Promise<TranscriptTurn[]> {
    const response = await request(`/api/sessions/${encodeURIComponent(sessionId)}/transcript`);
    return (await response.json()) as TranscriptTurn[];
  },

  async renameSession(id: string, title: string): Promise<void> {
    await request(`/api/sessions/${encodeURIComponent(id)}`, jsonInit("PATCH", { title }));
  },

  async deleteSession(id: string): Promise<void> {
    await request(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  async archiveSession(id: string): Promise<void> {
    await request(`/api/sessions/${encodeURIComponent(id)}/archive`, { method: "POST" });
  },

  async unarchiveSession(id: string): Promise<void> {
    await request(`/api/sessions/${encodeURIComponent(id)}/unarchive`, { method: "POST" });
  },

  async rescopeSession(id: string, domainScopes: string[]): Promise<void> {
    await request(
      `/api/sessions/${encodeURIComponent(id)}/scope`,
      jsonInit("POST", { domain_scopes: domainScopes }),
    );
  },

  // POST /api/chat streams the agent turn as SSE; the body is a ReadableStream
  // (EventSource is GET-only and can't carry a request body). Yields each parsed
  // ChatEvent so the caller renders text/tool activity live.
  async *chat(body: ChatRequest, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
    // `signal` lets the composer's Stop button abort the turn: aborting the fetch
    // closes the SSE connection, which the backend sees as a client disconnect and
    // unwinds the run cleanly (applies to every provider, not just local models).
    const response = await request("/api/chat", {
      ...jsonInit("POST", body),
      ...(signal ? { signal } : {}),
    });
    // Surface the run id before the body guard so Stop can cancel server-side even
    // if the stream itself carries nothing (the turn runs detached either way).
    const runId = response.headers.get("X-Run-Id");
    if (runId) yield { type: "run", run_id: runId };
    if (!response.body) return;
    yield* parseChatStream(response.body);
  },

  /** Reconnect to an in-flight turn whose stream dropped (a backgrounded socket) and
   * resume its live events from `after` — the count of server frames already folded — so
   * thinking/render progress picks up live. Throws (404 via request) once the run is no
   * longer live, so the caller falls back to the transcript. No synthetic run event: the
   * caller already holds the run id. */
  async *chatResume(runId: string, after: number, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
    const response = await request(
      `/api/chat/runs/${encodeURIComponent(runId)}/stream?after=${after}`,
      { ...(signal ? { signal } : {}) },
    );
    if (!response.body) return;
    yield* parseChatStream(response.body);
  },

  /** Cancel the in-flight chat turn (the composer's Stop). The turn runs detached
   * from the SSE stream server-side, so aborting the fetch no longer stops it — this
   * explicit signal does. Best-effort/idempotent on the server. */
  async cancelChatRun(runId: string): Promise<void> {
    await request(`/api/chat/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
  },

  // `sessionId` scopes the review inbox to a Full Brain chat: its own staged
  // proposals plus the session-less background ones. Omit it for the full list.
  async listProposals(sessionId?: string): Promise<ProposalSummary[]> {
    const path = sessionId
      ? `/api/proposals?session_id=${encodeURIComponent(sessionId)}`
      : "/api/proposals";
    const response = await request(path);
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

  // --- Location (Phase 7) — owner-only. The phones write via OwnTracks; these
  // read the slice back and manage device keys. ---

  async listLocationDevices(): Promise<DeviceSummary[]> {
    const response = await request("/api/locations/devices");
    return (await response.json()) as DeviceSummary[];
  },

  async listLocationTimeline(): Promise<TimelineEntry[]> {
    const response = await request("/api/locations/timeline");
    return (await response.json()) as TimelineEntry[];
  },

  async listLocationFixes(subjectId: string, since: string, until: string): Promise<LocationFix[]> {
    const params = new URLSearchParams({ subject_id: subjectId, since, until });
    const response = await request(`/api/locations/fixes?${params}`);
    return (await response.json()) as LocationFix[];
  },

  async listLocationPlaces(): Promise<PlaceGeofence[]> {
    const response = await request("/api/locations/places");
    return (await response.json()) as PlaceGeofence[];
  },

  // The compute-on-read place digest (L7a): week (default) or night. Owner-only;
  // names + times only, no coordinates. Recomputed each call — there is no feed.
  async locationDigest(period: "week" | "night" = "week"): Promise<LocationDigest> {
    const response = await request(`/api/locations/digest?period=${period}`);
    return (await response.json()) as LocationDigest;
  },

  // The owner's own current/last-known presence (L7b) for the app-open toast.
  async locationPresence(): Promise<LocationPresence> {
    const response = await request("/api/locations/presence");
    return (await response.json()) as LocationPresence;
  },

  async reverseGeocode(lat: number, lon: number): Promise<string | null> {
    const params = new URLSearchParams({ lat: String(lat), lon: String(lon) });
    const response = await request(`/api/locations/geocode?${params}`);
    return ((await response.json()) as { address: string | null }).address;
  },

  // Mint a one-time pairing code for the JBrain360 app. The returned `payload`
  // embeds the server URL, so the phone needs nothing configured — paste/scan it.
  // With `deviceId` the code RE-PAIRS that existing phone: redeeming it rotates the
  // phone's key in place (the way "roll the token" / "rotate the key" works for a
  // paired phone, which can only receive credentials by redeeming a code).
  async mintPairingCode(label: string, monitoring = 1, deviceId?: string): Promise<PairingCode> {
    const body: { label: string; monitoring: number; device_id?: string } = { label, monitoring };
    if (deviceId) body.device_id = deviceId;
    const response = await request("/api/pairing/codes", jsonInit("POST", body));
    return (await response.json()) as PairingCode;
  },

  async renameDevice(id: string, label: string): Promise<void> {
    await request(`/api/devices/${encodeURIComponent(id)}/rename`, jsonInit("POST", { label }));
  },

  async revokeDevice(id: string): Promise<void> {
    await request(`/api/devices/${encodeURIComponent(id)}/revoke`, { method: "POST" });
  },

  async deleteDevice(id: string): Promise<void> {
    await request(`/api/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // --- Member dashboard (JBrain360): the device-cookie-scoped family surface.
  // The device key lives in the Android Keystore and is exchanged for the session
  // cookie natively (POST /api/session/mint), so the web app never holds it — it
  // only reads back what the cookie's subject + family group may see.

  async memberRoster(): Promise<MemberSubject[]> {
    const response = await request("/api/member/roster");
    return (await response.json()) as MemberSubject[];
  },

  async memberPositions(subjectId: string, since: string, until: string): Promise<LocationFix[]> {
    const params = new URLSearchParams({ subject_id: subjectId, since, until });
    const response = await request(`/api/member/positions?${params}`);
    return (await response.json()) as LocationFix[];
  },

  async memberPlaces(): Promise<PlaceGeofence[]> {
    const response = await request("/api/member/places");
    return (await response.json()) as PlaceGeofence[];
  },

  async memberTimeline(): Promise<TimelineEntry[]> {
    const response = await request("/api/member/timeline");
    return (await response.json()) as TimelineEntry[];
  },
};
