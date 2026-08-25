// Single fetch wrapper for the backend API. Auth is a httpOnly session
// cookie, so every request sends credentials and a 401 anywhere means the
// session is gone — the app-level handler flips back to the login screen.
// Types are hand-written until Phase 1 introduces OpenAPI-generated clients
// (docs/reference/DEVELOPMENT.md, "Code standards / TypeScript").
//
// `npm run dev:mock` (VITE_MOCK=1) swaps the transport for in-memory
// fixtures so UI work never needs a backend (docs/reference/DESIGN.md, "UI
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
  LiveRun,
  ProposalDetail,
  ProposalSummary,
  SessionCreate,
  TranscriptTurn,
} from "../agent/types";
import type {
  IntakeConfig,
  IntakeConfigPatch,
  IntakeConfirmOut,
  IntakeLink,
  IntakeMintRequest,
  IntakeMintResult,
  IntakeSessionRow,
  IntakeSubmission,
  IntakeSubmissionDetail,
} from "../intake/types";
import type {
  ExternalMint,
  ExternalSession,
  JcodeModelStatus,
  JcodePowerStatus,
  JcodePreview,
  JcodeSession,
  JcodeShare,
  JcodeShareToken,
  NewSessionInput,
} from "../jcode/types";
import type {
  JlaunchJob,
  JlaunchShare,
  JlaunchShareToken,
  JlaunchSpec,
  PublicJlaunchRun,
} from "../jlaunch/types";

export interface Principal {
  principal_id: string;
  kind: string;
  label: string;
}

/** A host-vitals sample (GET /api/ops/vitals, and each frame of its stream).
 *  `gpu_busy_percent` is null when the box exposes no amdgpu gauge — "no telemetry
 *  here", which the top bar renders as an absent reading rather than an idle GPU. */
export interface HostVitals {
  gpu_busy_percent: number | null;
  /** The model coming onto the box right now, or null. Rides the vitals reading because
   *  it answers the same question at the same cadence — "what is the box doing this
   *  second" — and so needs no stream, poll, or endpoint of its own. */
  loading?: ModelLoad | null;
}

/** Work the box is doing on the GPU right now, which a status line shows in the present
 *  tense. `percent` (0..1) is null when nothing has measured it yet — on a box with no
 *  page-cache reading, or for a model the catalog cannot size — which the surfaces render
 *  as "working, no figure", never as 0%. */
export interface ModelLoad {
  model: string;
  /** When it started, so a status line counts up from THIS work rather than from
   *  whatever phase it interrupted. */
  at_ms: number;
  percent: number | null;
  /** `model_load` covers both halves of a load — the weights read, then the priming
   *  warm-up. `prefill` is a turn eating a long prompt with no load involved. Absent on
   *  an older box, which is read as a load. */
  kind?: "model_load" | "prefill";
}

/** What a turn was asked to do, resolved at its start (backend migration 0166). Every
 *  field is optional — a run predating the column, or one whose driver doesn't stamp,
 *  still lists and renders whatever it has. There is no verbatim prompt here: it is
 *  assembled per model call and stored nowhere. */
export interface TurnCall {
  provider?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  context_window?: number | null;
  vision?: boolean | null;
  persona?: string | null;
  /** null is the registry WILDCARD ("every in-scope tool"), not the empty allowlist. */
  tools?: string[] | null;
  user_message?: string | null;
  user_message_truncated?: boolean | null;
  label?: string | null;
}

/** One run in flight, for the vitals detail roster. `parent_run_id` nests a
 *  deep-research fan's children under the turn that spawned them. */
export interface LiveTurn {
  id: string;
  kind: string;
  status: string;
  name: string;
  started_at: string;
  /** Null while the turn is running; set once it settles. The roster splits on it, and
   *  `elapsed_ms` is the total duration rather than time-so-far once it is set. */
  ended_at: string | null;
  elapsed_ms: number;
  step_count: number;
  cost_tokens: number;
  progress_note: string | null;
  parent_run_id: string | null;
  session_id: string | null;
  domain_code: string | null;
  ran_as: string;
  prompt_version: string | null;
  /** The pipeline of the trigger that fired it; null means the owner started it. */
  trigger_pipeline: string | null;
  call: TurnCall | null;
}

/** One step of a turn's trail. `ok` is null while the step is still in flight — a
 *  running tool call, not a failed one. */
export interface TurnStep {
  idx: number;
  kind: string;
  name: string;
  ok: boolean | null;
  cost_tokens: number;
  error: string | null;
}

/** A turn's raw output. `live` says it came from the in-flight render accumulator (a
 *  parent /chat turn mid-answer) rather than the stored transcript, so the surface can
 *  label a streaming turn honestly instead of implying a finished answer. */
export interface TurnOutput {
  live: boolean;
  answer: string;
  reasoning: string;
  steps: TurnStep[];
}

/** The assembled prompt a run last sent to the model. Kept in memory server-side for
 *  the life of the process, so it is absent for a run that predates a restart, was
 *  evicted by a newer one, or never reached a model call. */
export interface CapturedPrompt {
  system: string;
  messages: { role: string; content: string }[];
  tools: string[];
  round_index: number;
  truncated: boolean;
  system_chars: number;
  message_chars: number;
}

export interface TurnDetail {
  steps: TurnStep[];
  output: TurnOutput | null;
  prompt: CapturedPrompt | null;
}

/** One second of recorded GPU load. `gpu` is null where the gauge could not be read —
 *  a gap in the plot, never a zero. */
export interface VitalsHistorySample {
  at_ms: number;
  gpu: number | null;
}

/** One thing the box DID that the GPU trace can be read against — a model load, the
 *  eviction that made room for it, an image render. `ended_ms` is null while it is still
 *  happening, which is what lets the surface say "loading gpt-oss-120b…" during the spike
 *  instead of accounting for it a minute later. */
export interface BoxEvent {
  at_ms: number;
  ended_ms: number | null;
  kind: string;
  /** The model or image model it happened to. */
  subject: string;
  /** WHY, when the caller knew — "to make room for gpt-oss-120b", "you unloaded it". */
  detail: string;
  /** running | ok | failed | stale — `stale` is a row whose process died mid-load. */
  status: string;
  /** Which half of the box did it: "api" or "worker". */
  source: string;
  /** How far a still-running load has got (0..1). Absent on settled rows — a finished
   *  load has no fraction to report. */
  percent?: number | null;
}

export interface LiveTurns {
  turns: LiveTurn[];
  /** Carried with the roster so the surface can say "busy box, no turns" in one
   *  breath — GPU busy covers the whole box, image generation included. */
  gpu_busy_percent: number | null;
}

/** A deferred media analysis' live state (GET /api/chat/deferred/{id}), polled by the
 * task_status card. `result` is the finished video_analysis card data once `status` is
 * `done`; `progress` drives the bar/label while running. */
export interface DeferredResult {
  result_id: string;
  status: "running" | "done" | "failed" | "canceled";
  progress: { step?: number; total?: number; label?: string };
  result: Record<string, unknown> | null;
  error: string | null;
  /** ISO start time (row creation) — the card's elapsed timer reads this so a reopen
   * mid-run shows true elapsed, not time-since-reopen. */
  started_at?: string;
}

/** A provisioned device with its location activity (GET /api/locations/devices).
 * `id` is the device's subject id; activity fields are null until its first fix. */
export interface DeviceSummary {
  id: string;
  label: string;
  created_at: string;
  revoked: boolean;
  last_seen: string | null;
  /** Seconds since the latest fix; null until the device reports one. */
  age_seconds: number | null;
  /** True when an active device has gone dark past the liveness horizon — its
   * tracker may have been killed in the background. */
  silent: boolean;
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

/** One host setting the app depends on, and whether that dependency currently holds.
 *  Mirrors `jbrain.host_settings.HostSetting`. */
export interface HostSetting {
  key: string;
  current: string;
  expected: string;
  ok: boolean;
  /** What breaks when it is wrong — the reason the row is worth showing. */
  impact: string;
  /** What the owner has to do about it. Empty when there is nothing to do. */
  remedy: string;
  /** Applying it needs a shell the owner does not have, so no button will help. */
  needs_host: boolean;
}

export interface HostSettings {
  settings: HostSetting[];
  ok: boolean;
  needs_host: string[];
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
  /** APU/SoC package power in watts (amdgpu power1_average), or null when absent. */
  apu_power_w: number | null;
  /** iGPU unified-memory usage (bytes): GTT is the bulk of a loaded model's device
   * footprint, which no process RSS shows. null off AMD / when /sys isn't exposed. */
  gpu_mem: {
    gtt_used_bytes: number;
    gtt_total_bytes: number;
    vram_used_bytes: number;
    vram_total_bytes: number;
  } | null;
  /** Curated /proc/meminfo lines (bytes) — Cached/Buffers/Shmem/… — so the memory
   * breakdown can name the reclaimable cache the "used" total folds in. null when
   * meminfo is unreadable. Keys are the raw meminfo labels. */
  mem_breakdown: Record<string, number> | null;
  containers: { service: string; mem_bytes: number }[];
  /** Per-process RSS (via the supervisor's `docker top`), biggest first — the raw
   * breakdown behind each container, e.g. the local-llm container's separate
   * llama-server per loaded model. Empty when the supervisor predates /processes. */
  processes: { service: string; pid: number; rss_bytes: number; command: string }[];
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
 * any field is null when the host didn't report it (e.g. no GPU/fans). The `_max`
 * fields carry the bucket's peak (the line is the average) so a spike shorter than
 * a bucket still shows as the chart's peak band rather than being averaged away. */
export interface MetricPoint {
  t: string;
  load_1m: number | null;
  load_1m_max: number | null;
  load_5m: number | null;
  load_15m: number | null;
  mem_used_bytes: number | null;
  mem_used_max_bytes: number | null;
  mem_total_bytes: number | null;
  swap_used_bytes: number | null;
  disk_used_bytes: number | null;
  disk_used_max_bytes: number | null;
  disk_total_bytes: number | null;
  gpu_busy_percent: number | null;
  gpu_busy_max: number | null;
  fan_rpm_max: number | null;
  power_w: number | null;
  power_w_max: number | null;
  /** Network + disk throughput in bytes/sec — the bucket PEAK (max), so spikes
   * survive downsampling. null before the first post-restart sample has a prior
   * counter to diff (or when the host didn't report a counter). */
  net_rx_bps: number | null;
  net_tx_bps: number | null;
  disk_read_bps: number | null;
  disk_write_bps: number | null;
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
  // Stream real prompt/answer text to the on-box wall display (:8800). OFF by
  // default — it puts owner text on the unauthenticated display, so only turn it on
  // for a display bound to the box's own monitor / localhost.
  brain_llm_stream: boolean;
  // Read the streamed wall-display turns aloud (Kokoro TTS on the box). OFF by
  // default — the runtime companion to brain_llm_stream, same localhost-only caveat.
  brain_read_aloud: boolean;
  // The Kokoro voice id the read-aloud speaks answers in — a voice id from
  // brainVoices() (e.g. "kokoro-af_heart"). The in-chat read-aloud renders each turn
  // on the box in this voice.
  brain_answer_voice: string;
  // Which engine the read-aloud renders with: "piper" (a legacy marker meaning on-box
  // Kokoro, falls back to the device's native voice when the box is unreachable) or
  // "native" (always the browser's own Web Speech voice).
  brain_read_aloud_engine: "piper" | "native";
  // Read-aloud voice effects (applied to the PWA read-aloud AND the wall display). speed is
  // Kokoro's native rate (0.5–2.0×); pitch (semitones, ±12) + chorus + robot are post-render
  // ffmpeg effects on the box (a native-voice read maps speed/pitch onto the Web Speech utterance).
  // Defaults are no-ops (1.0× / 0 / off).
  brain_answer_speed: number;
  brain_answer_pitch: number;
  brain_answer_chorus: boolean;
  brain_answer_robot: boolean;
  /** Whether an update rebuilds the model gateway onto the newest llama.cpp and then
   *  smoke-tests it by loading a model. ON by default. Surfaced because it lived only in
   *  the box's `.env`, which the owner has no terminal to reach. */
  local_llm_auto_update: boolean;
  /** Whether an update rebuilds the local inference engine with the Fast-Qwen-loads patch
   *  (patched llama-server → fast qwen MTP-hybrid disk restores). OFF by default. Surfaced
   *  because it lived only in the box's `.env`, which the owner has no terminal to reach. */
  local_llm_patch_restore_checkpoint: boolean;
  // The owner's read-aloud respelling map {word: "say it like"} — the api applies it as
  // a whole-word substitution before a clip renders. Empty by default.
  pronunciation_lexicon: Record<string, string>;
}

/** The on-box read-aloud engine's health (GET /api/brain/tts/health), driving the
 * Pronunciations panel's voice-engine chip. `g2p` is the grapheme-to-phoneme backend
 * the box resolved: "misaki" (best), "espeak" (degraded — respellings still apply,
 * quality limited), or "unavailable". The endpoint 503s when the box is unreachable,
 * so the client falls back to the all-off shape and the panel hides its chip. */
export interface BrainTtsHealth {
  kokoro_available: boolean;
  g2p: "misaki" | "espeak" | "unavailable";
  lexicon_entries: number;
  voice_count: number;
}

/** The read-only appointments ICS feed: enabled state + the URL token (owner-only). */
export interface FeedConfig {
  enabled: boolean;
  token: string | null;
}

/** The archivist's Gmail connection (GET /api/settings/gmail). Booleans only — the
 * secret/refresh token are stored server-side and never returned. */
export interface GmailSettings {
  client_id_set: boolean;
  client_secret_set: boolean;
  refresh_token_set: boolean;
  connected: boolean;
}

/** Partial credential write — omit a field to leave it unchanged. */
export interface GmailCredsPatch {
  client_id?: string;
  client_secret?: string;
  refresh_token?: string;
}

/** Result of POST /api/settings/gmail/test — did the saved credentials work. */
export interface GmailTestResult {
  ok: boolean;
  detail: string;
}

/** The hosted Tavily Extract tier (GET /api/settings/tavily). The API key is stored
 * server-side and NEVER returned — only whether one is present. `wired` = the tier
 * exists on this box; `effective` = it would actually run (wired AND enabled AND keyed). */
export interface TavilySettings {
  enabled: boolean;
  key_set: boolean;
  wired: boolean;
  effective: boolean;
}

/** Partial Tavily write — omit a field to leave it unchanged. Send a non-empty `api_key`
 * to set the key; `clear_key` removes the stored key (reverting to the env fallback). */
export interface TavilyPatch {
  enabled?: boolean;
  api_key?: string;
  clear_key?: boolean;
}

/** Result of POST /api/settings/tavily/test — did the live Tavily tier read a page. */
export interface TavilyTestResult {
  ok: boolean;
  detail: string;
}

/** jmolt's Moltbook account + operating switches (GET /api/settings/moltbook). The bearer
 * key is stored server-side and NEVER returned — only whether one is set. `autonomy` is
 * the queue-vs-auto switch (default off); `killed` is the global pause; `disclosure` is
 * the fixed honest bio header. */
export interface MoltbookSettings {
  key_set: boolean;
  handle: string;
  autonomy: boolean;
  killed: boolean;
  disclosure: string;
  /** The integrity watcher's last-observed account state: "ok" | "suspended" |
   * "moderated" | "tamper" — surfaced so the panel can show why writing paused. */
  account_state: string;
  /** Consecutive verification-failure streak (M11); at the limit all writes stop. */
  verify_fail_streak: number;
}

/** One staged write in jmolt's review queue (GET /api/settings/moltbook/outbox). `payload`
 * is jmolt-authored / third-party content — render it as INERT TEXT only (M15), never as
 * HTML. */
export interface MoltbookOutboxItem {
  id: string;
  kind: string;
  status: string;
  publish_at: string | null;
  payload: Record<string, unknown>;
  error: string | null;
}

/** Partial Moltbook write — omit a field to leave it unchanged. `clear_key` disconnects
 * the account (reverts to the env fallback); `clear_streak` resets the verify-fail streak
 * so writing resumes after the owner has looked. */
export interface MoltbookPatch {
  autonomy?: boolean;
  killed?: boolean;
  disclosure?: string;
  clear_key?: boolean;
  clear_streak?: boolean;
}

/** Result of POST /api/settings/moltbook/register — non-secret claim material only (the
 * key is never returned). The owner opens `claim_url` to verify email + post the X
 * verification tweet that activates the account. */
export interface MoltbookRegisterResult {
  claim_url: string;
  verification_code: string;
  handle: string;
}

/** Live claim status from Moltbook (pending_claim | claimed | …). */
export interface MoltbookClaimStatus {
  status: string;
}

// ----- Debug-console capability tokens (owner mints; an assistant uses) -----

export interface DebugToken {
  id: string;
  label: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  suspended_at: string | null;
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
  /** Provisioned on the box (weights installed) — it CAN be made available. */
  enabled: boolean;
  /** Effective-available to the router: provisioned AND not toggled off by the operator.
   * Only these show in the Available/Resident tabs and can be staged/loaded. */
  available: boolean;
  /** Queued for install from the PWA but not yet on the box — the next update
   * provisions it. Mutually exclusive with `enabled`. */
  queued: boolean;
  /** Queued for uninstall: a provisioned model the operator has asked to remove
   * from LOCAL_MODELS (and prune its weights) on the next update. True only while
   * still `enabled`; the flag clears once the update drops it from the catalog. */
  remove_queued: boolean;
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
  /** Bytes on disk for the model's dir (partial downloads included), in GB, or null
   * when nothing is downloaded / hosting off. Numerator of the live install bar:
   * download_gb / size_gb is the percentage while a queued model provisions. */
  download_gb: number | null;
  note: string;
  /** The model's catalog default context window — the gateway's `-c` absent an
   * override (the picker's "no override" value). */
  context_window: number;
  /** The model's native maximum window — the ceiling the size picker caps at, so
   * the operator can raise `-c` toward what the weights support. */
  max_context_window: number;
  /** The operator's per-model override (tokens), or null to use the default. */
  context_window_override: number | null;
  /** Estimated KV-cache GB at the effective window AND slot count — a second slot doubles it. */
  kv_gb: number;
  /** llama-server `-np` slot count: 1 (single slot, default) or 2 (a dedicated interactive
   * keep-warm slot beside the background one, so a primed chat prefix isn't evicted by
   * title/background traffic). Editable only while the model isn't resident. */
  parallel_slots: number;
  /** `--image-min-tokens`: the floor an image is encoded to, and the knob for whether small
   * text in a photo survives to the model. null on a text-only entry — no projector, so a
   * floor would do nothing and the control is not rendered. */
  image_min_tokens: number | null;
  /** The catalog's own floor, so the drawer can mark it "(default)" and persist null for it
   * instead of a redundant override row. */
  image_min_tokens_default: number | null;
}

/** One model a staged load would evict — catalog id, label, and resident footprint (GB),
 * so the screen can mark it on the memory bar during the preview. */
export interface EvictionVictim {
  id: string;
  label: string;
  gb: number;
}

/** The dry-run for the "stage" preview: what loading a model would evict right now, and
 * where the box would land — no side effects. `measured` is false when the box can't be
 * read (hosting off / gateway or meminfo down), so the screen just offers the load. */
export interface LoadPlan {
  model_id: string;
  measured: boolean;
  already_resident: boolean;
  fits: boolean;
  /** Even evicting everything leaves it over the free-RAM floor — it takes the box alone. */
  over: boolean;
  /** Even evicting everything, it can't fit total RAM — the load is refused (Load 409s). */
  over_box: boolean;
  victims: EvictionVictim[];
  resident_gb: number;
  projected_gb: number;
  ceiling_gb: number;
  total_gb: number;
}

/** Result of an unload: the catalog ids still resident, and whether the gateway answered. */
export interface LoadedLocalModels {
  loaded: string[];
  reachable: boolean;
}

/** One option in the code-mode (jcode) model dropdown. */
export interface JcodeModelChoice {
  id: string;
  label: string;
}

/** The code-mode agent's model selector (a card on the LLM screen). */
export interface JcodeModelInfo {
  /** Code mode is enabled — the screen renders the card only when true. */
  enabled: boolean;
  /** The effective EXECUTOR model id the agent runs (stored override, else `default`). */
  model: string;
  /** The config default (JBRAIN_JCODE_MODEL) — the value when no override is set. */
  default: string;
  /** The planner selection (grok's `plan` subagent): a model id, or `planner_same`
   * meaning single-model (planner == executor, no separate model). */
  planner: string;
  /** The config split-planner default (JBRAIN_JCODE_PLANNER_MODEL) — the model the card
   * suggests when a separate planner is enabled. */
  planner_default: string;
  /** The magic value the planner select uses for its "Same as executor" option. */
  planner_same: string;
  /** Installed, tool-capable local models both dropdowns offer. */
  options: JcodeModelChoice[];
}

export interface LlmSettings {
  providers: LlmProvider[];
  reasoning_efforts: ReasoningEffort[];
  reasoning_default: ReasoningEffort;
  tasks: LlmTask[];
  local_hosting_enabled: boolean;
  local_models: LocalModelInfo[];
  /** Live unified-memory gauge for the drawer meter; null when hosting is off / off-Linux. */
  /** `cache_gb` is page cache, which `used_gb` INCLUDES — broken out so the meter can name it
   *  rather than folding it into the system segment (with `--no-mmap` it is mostly a second
   *  copy of model weights). Null when /proc/meminfo can't be read. */
  host_memory: { total_gb: number; used_gb: number; cache_gb?: number | null } | null;
  /** Why llama-swap is serving flags that no longer match the saved settings, or null when it
   *  is up to date. The re-stamp happens once, right before a load, and is best-effort — so
   *  without surfacing this a failed write leaves a model quietly running at a window the
   *  screen claims it is not. */
  gateway_config_error?: string | null;
  /** The residency free-RAM floor (headroom the evictor keeps free). `fraction` is the
   * effective value (override when set, else `default`); `override` is null when the
   * effective value is the config default. Always present; the card shows only when hosting
   * is on. */
  free_ram: { fraction: number; default: number; override: number | null };
  /** End-of-turn restore: true = the box puts displaced models back once a turn ends;
   * false = it stops, and a model comes back only when a turn actually needs it. */
  auto_restore: boolean;
  /** The client-side ceiling on a local call, in seconds. REPORTED, not settable — it is
   * env-only. Surfaced because it masquerades as a hung model: a cold prefill at a large
   * window can exceed it and the turn fails as a client timeout, with nothing saying the
   * model was still working. */
  local_llm_timeout_s?: number | null;
  /** Code mode's model selector. Always present; `enabled` gates the card. */
  jcode: JcodeModelInfo;
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
  /**
   * How many attachments this note will upload on follow-up requests. Lets the
   * server defer integration until they land, so a note-plus-image capture is
   * extracted once with its OCR text instead of a premature body-only pass.
   */
  attachments_expected?: number;
}

export interface NoteUpdate {
  body?: string;
  domain?: string;
  /** Explicit null clears the destination; an absent key leaves it. */
  destination?: string | null;
}

// ===== Phase 3: analysis, entities, review, LLM usage (docs/reference/ANALYSIS.md) =====

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

/** A jerv conversation plan (docs/archive/JERV_PLANNING_TOOL_PLAN.md). `status` is the
 * flag enum the card colors (`not_approved | approved | in_work`); `approved` is the
 * ONE transition jerv can't make itself. The continuation fields drive the in-card
 * auto-resume countdown: `continuation_due_at` is when the next step auto-fires (null =
 * none pending), `awaiting_owner` means jerv paused for the owner's call, and the
 * counters bound the chain. Every plan endpoint returns this shape. */
/** One appended entry in a plan's step-results scratchpad (append-only, index-ordered). */
export interface PlanResult {
  heading?: string;
  note: string;
  /** Wall-clock seconds the step took (start of its turn → result recorded), when timed. */
  secs?: number;
}

export interface PlanOut {
  /** The plan's own identity — a card polls/mutates by it; many plans may share a session. */
  plan_id: string;
  session_id: string;
  body: string;
  status: string;
  updated_at: string;
  /** The append-only step-results scratchpad, in append order — the card's Results section. */
  results: PlanResult[];
  continuation_due_at: string | null;
  awaiting_owner: boolean;
  continuations_used: number;
  max_continuations: number;
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
  | "confirm_entity"
  | "wiki_contradiction"
  | "wiki_stale_claim";

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
  /** Lifetime token total: a full sum of the never-pruned usage ledger. */
  all_time: UsageTotals;
  by_task: TaskUsage[];
  days: DayUsage[];
}

// ===== Phase 5: the workflow run log (the Ops "Runs" surface; /api/runs) =====
// Mirrors backend/src/jbrain/api/runs.py 1:1 — the run log is owner-only.

/** A run's lifecycle state as stored (migration 0016 CHECK). 'error' is the
 * failed state; the Runs surface renders it as the red "failed" tile/dot. */
// 'queued' is a derived display state (no stored value): an in-flight pipeline run
// whose steps have not started yet, waiting behind the single-threaded worker.
// 'superseded' is a terminal state a scheduler fire stamps on a pipeline's prior
// still-running run when it fires again ("latest run wins") — neither done nor an
// error; the Runs surface renders it as a quiet, muted terminal tile/dot.
export type RunStatus = "queued" | "running" | "done" | "error" | "superseded";

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

/** The Runs dashboard's tile + chip-count aggregates (GET /api/runs/stats),
 * computed server-side over the whole log — so the tiles reflect the day, not the
 * fetched page. `by_kind` respects the surface's active date-range + hide-sweeps. */
export interface RunStats {
  active: number;
  failed_today: number;
  tokens_today: number;
  /** Keyed by chip bucket: agent | integration | pipeline. */
  by_kind: Record<string, number>;
}

/** The server-side filters the Runs surface drives (GET /api/runs). */
// ===== The Research Library (docs/archive/RESEARCH_LIBRARY_PLAN.md; DESIGN.md "Research
// Library") — owner browse over jerv's persisted deep-research reports + analysed videos
// (the external corpus). Reports key on their uuid; videos key on video_id. =====

export interface ReportListItem {
  id: string;
  question: string;
  /** The short LLM-generated display heading; null until the title job lands (fall back
   * to the question). */
  title: string | null;
  complexity: string;
  created_at: string | null;
  sub_agents: number;
  rounds: number;
  /** The owner's folder this report is filed under; null = the trailing "Ungrouped"
   * section. Owner-only browse metadata the Reports tab groups by. */
  group_id: string | null;
  /** `web` | `library` | `library_first` — flags a report drawn from private notes, so the
   * Share sheet can warn before publishing it. */
  source_mode: string;
  /** When this report auto-expires (ISO); null = keep forever. The Reports tab shows
   * "expires in N days" and offers Keep (REPORT_EXPIRY_PLAN.md). */
  expires_at: string | null;
}

/** An owner-named folder on the Research Library's Reports tab (mirrors TaskGroup). */
export interface ReportGroup {
  id: string;
  name: string;
  position: number;
}
export interface ReportHit {
  id: string;
  question: string;
  excerpt: string;
}
export interface ReportDetail {
  id: string;
  question: string;
  report_md: string;
  complexity: string;
  rounds: number;
  sub_agents: number;
  analyzed: boolean;
  revised: boolean;
  coverage_limited: boolean;
  truncated: boolean;
  sources: { url?: string; title?: string }[];
  created_at: string | null;
}
/** A minted share link — the secret is returned ONCE (build the /share/<token> URL from it). */
export interface MintedShare {
  id: string;
  target_kind: "report" | "group";
  label: string | null;
  token: string;
  /** The target is a report synthesised from private notes — warn before sending. */
  library_warning: boolean;
}
/** A live share link in the owner's list — metadata + usage, never the secret. */
export interface ShareLinkView {
  id: string;
  label: string | null;
  target_kind: "report" | "group";
  report_id: string | null;
  group_id: string | null;
  created_at: string | null;
  last_viewed_at: string | null;
  view_count: number;
  library_warning: boolean;
}
/** One entry in a public folder-share index. */
export interface ShareReportItem {
  id: string;
  question: string;
  title: string | null;
  created_at: string | null;
}
/** The public resolve of a share token: one report, or a folder label + its members. */
export interface ShareView {
  kind: "report" | "group";
  label: string | null;
  report: ReportDetail | null;
  reports: ShareReportItem[] | null;
}
export interface VideoListItem {
  video_id: string;
  provider: string;
  title: string;
  channel_name: string;
  url: string;
  published_at: string | null;
  duration_s: number | null;
}
export interface VideoHit {
  /** The stable public key the browse surface opens/deletes a hit by (matches the row + detail). */
  video_id: string;
  source_id: string;
  title: string;
  channel_name: string;
  url: string;
  passage: string;
  t_ms: number | null;
}
export interface TranscriptWindow {
  t_ms: number;
  text: string;
}
export interface VideoDetail {
  source_id: string;
  video_id: string;
  provider: string;
  title: string;
  channel_name: string;
  url: string;
  transcript_source: string;
  summary: string;
  duration_s: number | null;
  duration_ms: number | null;
  published_at: string | null;
  windows: TranscriptWindow[];
  // `thumb_data_uri` is the inline frame still the server redeems from the stored thumb_id
  // (absent when its blob was purged — the frame then renders as a bare marker).
  frames: { t_ms?: number; caption?: string; thumb_id?: string; thumb_data_uri?: string }[];
  cued_transcript: {
    text?: string;
    words?: { text: string; start_ms: number; end_ms: number; confidence?: number }[];
  } | null;
}
export interface ReportListResponse {
  items: ReportListItem[];
  total: number;
}
export interface ReportSearchResponse {
  items: ReportHit[];
  degraded: boolean;
}
export interface VideoListResponse {
  items: VideoListItem[];
  total: number;
}
export interface VideoSearchResponse {
  items: VideoHit[];
  degraded: boolean;
}

export interface RunListParams {
  /** The enabled chip kinds to include (agent expands to agent+subagent); omit for all. */
  kinds?: string[];
  excludeSweeps?: boolean;
  /** ISO floor for the date-range filter; omit for all time. */
  since?: string;
  limit?: number;
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
  /** A live "processed X of Y" line while the run is in flight; null once it closes. */
  progress_note: string | null;
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
  /** The step's captured structured-log trace (the "full logs" view) — an array of
   * compact event objects, or null/absent for a step that recorded none. */
  detail?: Record<string, unknown>[] | null;
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
  /** A live "processed X of Y" line while the run is in flight; null once it closes. */
  progress_note: string | null;
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
 * or schedule; `group` buckets it into the surface's sections by subject
 * (note lifecycle / wiki / background maintenance). */
export interface Automation {
  trigger_id: string;
  kind: "on_event" | "schedule";
  group: "note" | "wiki" | "maintenance";
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
  /** The task-style schedule spec the owner edits on a sweep card (null for events).
   * `interval` keeps the legacy fixed cadence; on_demand/once/repeat mirror a task. */
  schedule_kind: ScheduleSpecKind | null;
  schedule_freq: ScheduleFreq | null;
  schedule_days: number[];
  schedule_time: string | null;
  run_at: string | null;
  timezone: string | null;
}

/** A schedule's timing kind: the legacy fixed `interval` plus the task spec kinds. */
export type ScheduleSpecKind = "interval" | "on_demand" | "once" | "repeat";

/** PUT /api/ops/schedules/{id} body — replace a schedule's timing spec. Mirrors the
 * backend's ScheduleBody; cross-field rules are enforced server-side. */
export interface ScheduleInput {
  schedule_kind: ScheduleSpecKind;
  interval_seconds: number | null;
  schedule_freq: ScheduleFreq | null;
  schedule_days: number[];
  schedule_time: string | null;
  run_at: string | null;
  timezone: string;
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

// ===== Tasks: saved prompts that spawn an agent session (docs/mocks/tasks-launcher) =====

export type TaskAgent = "jerv" | "curator" | "teacher" | "archivist";
export type ScheduleKind = "on_demand" | "once" | "repeat";
export type ScheduleFreq = "daily" | "weekdays" | "weekly";

/** A saved task — a prompt + persona + schedule + delivery. `next_run_at`/`last_run_at`
 * are server-computed; the editor sends only the spec. */
/** An owner-named bucket the Tasks surface sorts tasks into (GUI Direction B —
 * docs/mocks/task-grouping/). `position` orders the buckets themselves. */
export interface TaskGroup {
  id: string;
  name: string;
  position: number;
}

export interface Task {
  id: string;
  /** The owner-named bucket this task sits in; null = the trailing "Ungrouped"
   * section. `position` is its 0-based rank within that bucket. Both are written by
   * the reorder endpoint (a within-group drag or a "Move to…"). */
  group_id: string | null;
  position: number;
  name: string;
  prompt: string;
  agent: TaskAgent;
  domain_scopes: string[];
  schedule_kind: ScheduleKind;
  schedule_freq: ScheduleFreq | null;
  /** Sunday=0 … Saturday=6, for a weekly repeat. */
  schedule_days: number[];
  /** "HH:MM" local time for a repeat. */
  schedule_time: string | null;
  /** ISO instant for a one-off. */
  run_at: string | null;
  timezone: string;
  enabled: boolean;
  notify_push: boolean;
  home_card: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  /** The most recent run, embedded by the server so the card's "latest result" band
   * renders (and opens its session) without a per-card fetch. Null until first run. */
  latest_run: TaskRun | null;
}

/** The create/replace payload — the spec the editor authors (server computes the rest). */
export interface TaskInput {
  name: string;
  prompt: string;
  agent: TaskAgent;
  domain_scopes: string[];
  schedule_kind: ScheduleKind;
  schedule_freq: ScheduleFreq | null;
  schedule_days: number[];
  schedule_time: string | null;
  run_at: string | null;
  timezone: string;
  enabled: boolean;
  notify_push: boolean;
  home_card: boolean;
}

/** One execution of a task — links to the agent session it produced. */
export interface TaskRun {
  id: string;
  task_id: string;
  session_id: string | null;
  status: RunStatus;
  trigger: "schedule" | "manual";
  /** A short excerpt of the answer (owner-only). */
  summary: string;
  error: string | null;
  step_count: number;
  cost_tokens: number;
  started_at: string;
  ended_at: string | null;
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

// ----- JPet: the family wall pet (docs/archive/JPET_V3_PLAN.md) -----
/** One step of the pet's action script (wire mirror of the backend `Step`). */
export interface PetStep {
  action: string;
  target?: string;
  destination?: string;
  duration_ms?: number;
  emotion?: string;
}

export interface PetState {
  name: string;
  domain: string;
  mood: string;
  emotion: string;
  speech: string | null;
  asleep: boolean;
  pos_x: number;
  pos_z: number;
  target_x: number;
  target_z: number;
  facing: number;
  action: string;
  /** The pet's current colour (a named colour or "rainbow"); null = default palette. */
  color: string | null;
  /** The bounded, ordered action script the wall plays out (v2). */
  script: PetStep[];
  /** The room object the pet is currently holding, or null. */
  carrying: string | null;
  /** Day/night light state (the light_switch toggles it). */
  lights_on: boolean;
  /** Room props the pet can target/carry: {kind: [x, z]} in normalized floor coords. */
  objects: Record<string, [number, number]>;
}

/** A kid play-button (each expands to a canned script), `say` (freeform → talk brain),
 * `color` (recolour from the phone palette), or the parent `move` (send the pet to a raw
 * floor point). */
export type PetAction =
  | "dance"
  | "spin"
  | "jump"
  | "wave"
  | "wiggle"
  | "chase"
  | "hide"
  | "beep"
  | "come"
  | "sleep"
  | "wake"
  | "eat"
  | "lights"
  | "jumprope"
  | "music"
  | "guitar"
  | "sing"
  | "fart"
  | "burp"
  | "say"
  | "move"
  | "color";

export interface PetCommand {
  action: PetAction;
  /** Normalized floor coords in [-1, 1] — only read for `move`. */
  x?: number;
  z?: number;
  /** What the child said (`say`), or the colour name (`color`). */
  text?: string;
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
    // Surface the backend's actionable message (FastAPI's `{detail}`) instead of a
    // bare status — e.g. "the dreamshaper image model isn't installed…". Falls back
    // to the status when the body isn't a JSON detail.
    throw new ApiError(response.status, await errorDetail(response));
  }
  return response;
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.clone().json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
  } catch {
    // not a JSON body — fall through to the status line
  }
  return `Request failed: ${response.status}`;
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

/** A frame thumbnail from a chat attachment's analyze_video result. The backend
 * validates `thumbId` against the attachment's stored frames under the firewall. */
export function chatAttachmentThumbUrl(id: string, thumbId: string): string {
  return `/api/chat-attachments/${encodeURIComponent(id)}/thumb/${encodeURIComponent(thumbId)}`;
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

// The image-launcher gallery/render ids (mock.ts `mock-gallery-*`/`mock-render-*`)
// have no hand-authored placeholder, so derive a deterministic gradient SVG from
// the id — the same id always yields the same stand-in, offline. (Hex is image
// *content*, not theme styling.) Only reached in MOCK_MODE.
const MOCK_GALLERY_HUES: Array<[string, string]> = [
  ["#3a3550", "#6f5b8c"],
  ["#2c4a3a", "#5a8c6f"],
  ["#2c3a4a", "#5a7a8c"],
  ["#4a3a2c", "#8c6f5a"],
  ["#43304a", "#7a5a8c"],
  ["#2c4a47", "#5a8c86"],
];
function mockLauncherSvg(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  const [a, b] = MOCK_GALLERY_HUES[h % MOCK_GALLERY_HUES.length] ?? ["#3a3550", "#6f5b8c"];
  return svgDataUri(
    `<svg xmlns='http://www.w3.org/2000/svg' width='640' height='640'>
      <defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
        <stop offset='0' stop-color='${a}'/><stop offset='1' stop-color='${b}'/></linearGradient></defs>
      <rect width='640' height='640' fill='url(#g)'/>
      <circle cx='400' cy='270' r='100' fill='rgba(255,255,255,.10)'/>
      <rect x='100' y='384' width='350' height='100' rx='26' fill='rgba(0,0,0,.12)'/></svg>`,
  );
}

/** Source URL for the served result of a generated image (data-only contract:
 * the tool-view payload carries only `image_id`; the component builds this). */
export function generatedImageUrl(id: string): string {
  if (MOCK_MODE && MOCK_GENIMG[id]) return MOCK_GENIMG[id];
  if (MOCK_MODE && (id.startsWith("mock-gallery-") || id.startsWith("mock-render-")))
    return mockLauncherSvg(id);
  return `/api/images/generated/${encodeURIComponent(id)}`;
}

/** Source URL for a web citation's favicon — fetched and cached ON-BOX from the
 * site's host, so the chat shows a source logo without the client ever touching the
 * third-party host (invariant #9). A miss 404s and the chip falls back to a plain
 * initial. `host` is a bare hostname (the component derives it from the source URL). */
export function faviconUrl(host: string): string {
  return `/api/agent/favicon?host=${encodeURIComponent(host)}`;
}

/** Source ("before") URL for an edit: the original bytes the edit started from,
 * resolved by the same id on the backend. */
export function generatedImageSourceUrl(id: string): string {
  if (MOCK_MODE && MOCK_GENIMG["mock-genimg-lighthouse"] && id === "mock-genimg-lighthouse-stormy")
    return MOCK_GENIMG["mock-genimg-lighthouse"];
  return `/api/images/generated/${encodeURIComponent(id)}/source`;
}

// --- Image launcher (Wave L1): the standalone on-box generate/edit screen. The
// gallery shares the owner-only `generated_images` rows the jerv tools also write
// (docs/archive/IMAGE_LAUNCHER_PLAN.md). Mock-backed until the L3 direct render API.

/** One render in the gallery — the by-id summary the screen lists and reveals.
 * `seed` is the resolved seed (null only before a render is recorded). */
export interface GeneratedImageOut {
  id: string;
  kind: "generate" | "edit";
  prompt: string;
  width: number;
  height: number;
  model: string;
  seed: number | null;
  created_at: string;
}

export type ImageSpeed = "dreamshaper" | "fast" | "quality";
export type ImageAspect = "square" | "portrait" | "landscape" | "tall" | "wide";
export type ImageResolution = "small" | "medium" | "large";

/** The generate form's config (speed implies the model; aspect+resolution imply
 * the dims). `seed` blank = random; `negativePrompt` blank = none. */
export interface GenerateImageRequest {
  prompt: string;
  speed: ImageSpeed;
  aspect: ImageAspect;
  resolution: ImageResolution;
  steps: number;
  seed: number | null;
  negativePrompt: string;
}

/** The edit form's config. The source is a prior render (by id) or an uploaded
 * file (passed alongside, not on this object); aspect is inherited from the
 * source, so it carries no aspect knob. */
export interface EditImageRequest {
  prompt: string;
  speed: ImageSpeed;
  resolution: ImageResolution;
  steps: number;
  seed: number | null;
  negativePrompt: string;
  /** A prior render to edit, when the source isn't an uploaded file. */
  sourceImageId: string | null;
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

  // The Kokoro voice ids installed on the box ("kokoro-<voice>"), proxied through the api from
  // the on-box display. Empty when the display is unconfigured/unreachable — read-aloud then has
  // no voices and falls back to the device's own voice.
  async brainVoices(): Promise<string[]> {
    const response = await request("/api/brain/voices");
    const body = (await response.json()) as { voices?: unknown };
    return Array.isArray(body.voices)
      ? body.voices.filter((v): v is string => typeof v === "string")
      : [];
  },

  // Render `text` to a WAV in `voice` on the box's Kokoro (via the api proxy) — the audio
  // the in-chat read-aloud and the Settings "play sample" button play. `lead` (silence
  // pad, ms) is 0 on continuation chunks so a multi-clip reply plays gaplessly.
  async brainTts(
    voice: string,
    text: string,
    lead?: number,
    speed?: number,
    trail?: number,
    fx?: { pitch?: number; chorus?: boolean; robot?: boolean },
  ): Promise<Blob> {
    const params = new URLSearchParams({ voice, text });
    if (lead !== undefined) params.set("lead", String(lead));
    if (speed !== undefined) params.set("speed", String(speed));
    if (trail !== undefined) params.set("trail", String(trail));
    // Voice effects (the box applies them post-render); only send a non-default so a plain read
    // never spawns ffmpeg on the box.
    if (fx?.pitch) params.set("pitch", String(fx.pitch));
    if (fx?.chorus) params.set("chorus", "1");
    if (fx?.robot) params.set("robot", "1");
    const response = await request(`/api/brain/tts?${params.toString()}`);
    return response.blob();
  },

  // The on-box read-aloud engine's health (GET /api/brain/tts/health) — the Pronunciations
  // panel reads it for the voice-engine chip. Defensive: the endpoint 503s when the box is
  // unreachable and the body can be anything, so any error or malformed shape resolves to the
  // all-off fallback and the caller never has to catch.
  async brainTtsHealth(): Promise<BrainTtsHealth> {
    const fallback: BrainTtsHealth = {
      kokoro_available: false,
      g2p: "unavailable",
      lexicon_entries: 0,
      voice_count: 0,
    };
    try {
      const response = await request("/api/brain/tts/health");
      const body = (await response.json()) as Partial<BrainTtsHealth>;
      return {
        kokoro_available: body.kokoro_available === true,
        g2p: body.g2p === "misaki" || body.g2p === "espeak" ? body.g2p : "unavailable",
        lexicon_entries: typeof body.lexicon_entries === "number" ? body.lexicon_entries : 0,
        voice_count: typeof body.voice_count === "number" ? body.voice_count : 0,
      };
    } catch {
      return fallback;
    }
  },

  // The archivist's Gmail connection. Status is booleans only; saving a partial
  // patch leaves the other fields intact; test verifies the saved credentials.
  async getGmailSettings(): Promise<GmailSettings> {
    const response = await request("/api/settings/gmail");
    return (await response.json()) as GmailSettings;
  },

  async updateGmailSettings(patch: GmailCredsPatch): Promise<GmailSettings> {
    const response = await request("/api/settings/gmail", jsonInit("PUT", patch));
    return (await response.json()) as GmailSettings;
  },

  async testGmailSettings(): Promise<GmailTestResult> {
    const response = await request("/api/settings/gmail/test", { method: "POST" });
    return (await response.json()) as GmailTestResult;
  },

  // The hosted Tavily Extract recovery tier. Status hides the key (booleans only);
  // saving a patch leaves omitted fields intact; test runs the live tier against a URL.
  async getTavilySettings(): Promise<TavilySettings> {
    const response = await request("/api/settings/tavily");
    return (await response.json()) as TavilySettings;
  },

  async updateTavilySettings(patch: TavilyPatch): Promise<TavilySettings> {
    const response = await request("/api/settings/tavily", jsonInit("PUT", patch));
    return (await response.json()) as TavilySettings;
  },

  async testTavilySettings(url?: string): Promise<TavilyTestResult> {
    const response = await request(
      "/api/settings/tavily/test",
      jsonInit("POST", url ? { url } : {}),
    );
    return (await response.json()) as TavilyTestResult;
  },

  // jmolt's Moltbook account + switches. Status hides the key (key_set boolean only);
  // register runs the platform handshake and returns only claim material; patch flips
  // the autonomy/kill switches or disconnects the account.
  async getMoltbookSettings(): Promise<MoltbookSettings> {
    const response = await request("/api/settings/moltbook");
    return (await response.json()) as MoltbookSettings;
  },

  async updateMoltbookSettings(patch: MoltbookPatch): Promise<MoltbookSettings> {
    const response = await request("/api/settings/moltbook", jsonInit("PUT", patch));
    return (await response.json()) as MoltbookSettings;
  },

  async registerMoltbook(name: string, description: string): Promise<MoltbookRegisterResult> {
    const response = await request(
      "/api/settings/moltbook/register",
      jsonInit("POST", { name, description }),
    );
    return (await response.json()) as MoltbookRegisterResult;
  },

  async getMoltbookClaimStatus(): Promise<MoltbookClaimStatus> {
    const response = await request("/api/settings/moltbook/claim-status");
    return (await response.json()) as MoltbookClaimStatus;
  },

  // jmolt's review queue: the writes it staged, awaiting release (queued) or already
  // released-but-unsent. The owner releases or discards each while autonomy is off.
  async getMoltbookOutbox(): Promise<MoltbookOutboxItem[]> {
    const response = await request("/api/settings/moltbook/outbox");
    return (await response.json()) as MoltbookOutboxItem[];
  },

  async actOnMoltbookOutbox(id: string, action: "release" | "discard"): Promise<void> {
    await request(`/api/settings/moltbook/outbox/${id}`, jsonInit("POST", { action }));
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

  /** Set (or clear, with null) the model's `--image-min-tokens` floor — how much of an image
   * the model actually sees. Raise it when small text comes back garbled (a curved label, a
   * receipt); the cost is prefill and KV, not weights, so it does not move the load footprint.
   * Returns the full snapshot; the model unloads so its next request reloads with the new
   * floor. 422 on a model with no projector. */
  async setLocalImageMinTokens(id: string, tokens: number | null): Promise<LlmSettings> {
    const response = await request(
      `/api/settings/llm/local-models/${encodeURIComponent(id)}/image-min-tokens`,
      jsonInit("PUT", { image_min_tokens: tokens }),
    );
    return (await response.json()) as LlmSettings;
  },

  /** Set the model's llama-server `-np` slot count: 2 opts into a dedicated interactive
   * keep-warm slot (so a primed chat prefix survives background/title traffic); 1 (or null)
   * reverts to a single slot. A second slot roughly doubles the model's KV. Returns the
   * full snapshot; the model unloads so its next request reloads with the new `-np`. */
  async setLocalParallelSlots(id: string, slots: number | null): Promise<LlmSettings> {
    const response = await request(
      `/api/settings/llm/local-models/${encodeURIComponent(id)}/parallel-slots`,
      jsonInit("PUT", { slots }),
    );
    return (await response.json()) as LlmSettings;
  },

  /** Set (or clear, with null) the residency free-RAM floor — the fraction of RAM the
   * evictor keeps free before each local model load. null reverts to the config default;
   * else 0.05..0.5. Returns the full snapshot. */
  async setFreeRamFraction(fraction: number | null): Promise<LlmSettings> {
    const response = await request(
      "/api/settings/llm/free-ram-fraction",
      jsonInit("PUT", { fraction }),
    );
    return (await response.json()) as LlmSettings;
  },

  /** Turn the end-of-turn restore on or off. Off means the box stops putting back models a
   * displacement evicted, so nothing loads until a turn needs it. Returns the snapshot. */
  async setAutoRestore(enabled: boolean): Promise<LlmSettings> {
    const response = await request("/api/settings/llm/auto-restore", jsonInit("PUT", { enabled }));
    return (await response.json()) as LlmSettings;
  },

  /** Choose the model the code-mode (jcode) agent runs; "" reverts to the default.
   * Returns the full settings snapshot. */
  async setJcodeModel(model: string): Promise<LlmSettings> {
    const response = await request("/api/settings/llm/jcode-model", jsonInit("PUT", { model }));
    return (await response.json()) as LlmSettings;
  },

  /** Choose the planner model for code mode's grok `plan` subagent; "" reverts to the
   * config split default, `planner_same` collapses to a single model. Returns the snapshot. */
  async setJcodePlanner(planner: string): Promise<LlmSettings> {
    const response = await request("/api/settings/llm/jcode-planner", jsonInit("PUT", { planner }));
    return (await response.json()) as LlmSettings;
  },

  /** Mark a provisioned model available / unavailable to the router (keeps the weights);
   * returns the full snapshot. */
  async setLocalAvailable(id: string, available: boolean): Promise<LlmSettings> {
    const response = await request(
      `/api/settings/llm/local-models/${encodeURIComponent(id)}/available`,
      jsonInit("PUT", { available }),
    );
    return (await response.json()) as LlmSettings;
  },

  /** Dry-run the "stage" preview: what loading this model would evict right now (no side
   * effects). The Load button then commits it. */
  async planLoadLocalModel(id: string): Promise<LoadPlan> {
    const response = await request(
      `/api/settings/llm/local-models/${encodeURIComponent(id)}/plan-load`,
      { method: "POST" },
    );
    return (await response.json()) as LoadPlan;
  },

  /** Queue / unqueue an un-provisioned model for install on the next update;
   * returns the full snapshot. */
  async queueLocalInstall(id: string, on: boolean): Promise<LlmSettings> {
    const response = await request(
      `/api/settings/llm/local-models/${encodeURIComponent(id)}/install`,
      { method: on ? "POST" : "DELETE" },
    );
    return (await response.json()) as LlmSettings;
  },

  /** Queue / unqueue a provisioned model for uninstall on the next update (which
   * drops it from LOCAL_MODELS and prunes its weights); returns the full snapshot. */
  async queueLocalUninstall(id: string, on: boolean): Promise<LlmSettings> {
    const response = await request(
      `/api/settings/llm/local-models/${encodeURIComponent(id)}/uninstall`,
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

  async suspendDebugToken(id: string): Promise<void> {
    await request(`/api/settings/debug-tokens/${encodeURIComponent(id)}/suspend`, {
      method: "POST",
    });
  },

  async resumeDebugToken(id: string): Promise<void> {
    await request(`/api/settings/debug-tokens/${encodeURIComponent(id)}/resume`, {
      method: "POST",
    });
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

  // ----- Plans: the owner's approve/edit + auto-resume controls for a jerv plan.
  // All owner-only; the `plan_card` tool-view and its out-of-card status surfaces
  // (the composer plan pill + Chats badge) read this (JERV_PLANNING_TOOL_PLAN.md). -----
  // Fetch one specific plan by its id — an inline card polls this so it keeps showing ITS
  // plan (its own body/status/results), not whatever the conversation's current plan is.
  async getPlan(planId: string): Promise<PlanOut> {
    const response = await request(`/api/plans/${encodeURIComponent(planId)}`);
    return (await response.json()) as PlanOut;
  },

  // The conversation's ACTIVE (latest) plan — what the omnibox pill/popover tracks, so it
  // follows a newly-created plan instead of staying stuck on a superseded one. 404 → null.
  async getActivePlan(sessionId: string): Promise<PlanOut | null> {
    try {
      const response = await request(`/api/plans/session/${encodeURIComponent(sessionId)}/active`);
      return (await response.json()) as PlanOut;
    } catch (e) {
      // 404 = the conversation has no plan yet; any other error propagates.
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  },

  // The owner-only sign-off — the one transition jerv cannot make itself, so web
  // content it reads can never talk it into self-approving.
  async approvePlan(planId: string): Promise<PlanOut> {
    const response = await request(`/api/plans/${encodeURIComponent(planId)}/approve`, {
      method: "POST",
    });
    return (await response.json()) as PlanOut;
  },

  // Correct jerv's draft text in place before approving.
  async editPlan(planId: string, body: string): Promise<PlanOut> {
    const response = await request(
      `/api/plans/${encodeURIComponent(planId)}/edit`,
      jsonInit("POST", { body }),
    );
    return (await response.json()) as PlanOut;
  },

  // Cancel the pending auto-continuation (and reset its budget) — the owner halting
  // the loop from the card.
  async stopPlan(planId: string): Promise<PlanOut> {
    const response = await request(`/api/plans/${encodeURIComponent(planId)}/stop`, {
      method: "POST",
    });
    return (await response.json()) as PlanOut;
  },

  // Fire the next step now instead of waiting out the window — arms the continuation
  // due immediately (the server sweep picks it up within ~15s).
  async continuePlan(planId: string): Promise<PlanOut> {
    const response = await request(`/api/plans/${encodeURIComponent(planId)}/continue`, {
      method: "POST",
    });
    return (await response.json()) as PlanOut;
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

  // File the owner CORRECTION note behind "correct it" (provenance
  // owner_correction, the #7 channel) so the fix force-supersedes what it
  // corrects instead of colliding with it. The card is then resolved with the
  // returned note id via reviewResolve(id, "correct", { note_id }).
  async reviewFileCorrection(id: string, domain: string, body: string): Promise<{ id: string }> {
    const response = await request(
      `/api/review/${encodeURIComponent(id)}/correction`,
      jsonInit("POST", { body, domain }),
    );
    return { id: ((await response.json()) as { note_id: string }).note_id };
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

  /** Host settings the app assumes and cannot always apply. Exists because the one that
   *  mattered was invisible: `ttm.pages_limit` sat above total RAM — which disables it —
   *  and nothing said so until a freeze was traced back to it weeks later. */
  async opsHostSettings(): Promise<HostSettings> {
    const response = await request("/api/ops/host-settings");
    return (await response.json()) as HostSettings;
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

  /** Rebuild ONE service (compose build + up -d) via a supervisor one-shot — the
   * per-container "Rebuild" button, for applying a code/Dockerfile change already on the
   * box (e.g. a newly-baked tts-stt voice) without a full update. Throws
   * ApiError(409) if another one-shot is running, ApiError(404) for an unknown service. */
  async opsRebuildStart(service: string): Promise<{ oneshot: string }> {
    const response = await request("/api/ops/rebuild", jsonInit("POST", { service }));
    return (await response.json()) as { oneshot: string };
  },

  async opsRebuildStatus(): Promise<UpdateStatus> {
    const response = await request("/api/ops/rebuild/status");
    return (await response.json()) as UpdateStatus;
  },

  /** Start the local-model DOWNLOAD one-shot (the "Download" action): sync queued
   * weights without a system update. Throws ApiError(409) if a one-shot is already
   * running — the caller attaches to the existing run's status instead of failing. */
  async opsLocalProvisionStart(): Promise<{ oneshot: string }> {
    const response = await request("/api/ops/local-provision", { method: "POST" });
    return (await response.json()) as { oneshot: string };
  },

  async opsLocalProvisionStatus(): Promise<UpdateStatus> {
    const response = await request("/api/ops/local-provision/status");
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

  /** Stop / start a single container (docker stop/start on the existing container) —
   * the per-container power controls next to Restart. */
  async opsStop(service: string): Promise<void> {
    await request("/api/ops/stop", jsonInit("POST", { service }));
  },

  async opsStart(service: string): Promise<void> {
    await request("/api/ops/start", jsonInit("POST", { service }));
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

  /** One host-vitals reading. The top bar's PROBE: because EventSource cannot see a
   *  status code, this is how the meter learns whether this principal may read host
   *  telemetry at all — a family member gets the 403 here, once, instead of an
   *  EventSource retrying against it forever. */
  async opsVitals(): Promise<HostVitals> {
    const response = await request("/api/ops/vitals");
    return (await response.json()) as HostVitals;
  },

  /** The 1 Hz host-vitals stream behind the top bar's GPU trace. Open only after
   *  `opsVitals` has succeeded. Errors (and retries) in mock mode — like the log
   *  stream, live host telemetry is out of mock scope. */
  opsVitalsStream(): EventSource {
    return new EventSource("/api/ops/vitals/stream");
  },

  /** The runs in flight right now, with the call each was set up with, for the vitals
   *  detail roster. Polled while that surface is open and in the foreground. */
  /** The roster. `seconds` widens it from "running right now" (0) to "ran inside this
   *  window", so the list matches the range the graph is showing. */
  async opsTurns(seconds = 0): Promise<LiveTurns> {
    const response = await request(`/api/ops/turns?seconds=${seconds}`);
    return (await response.json()) as LiveTurns;
  },

  /** Report what the BROWSER saw of the vitals stream, so it can be read back through the
   *  debug token. Best-effort by contract — a diagnostic that breaks the screen it is
   *  diagnosing is worse than no diagnostic. */
  async opsReportClientVitals(report: Record<string, unknown>): Promise<void> {
    await request("/api/ops/client-vitals", jsonInit("POST", report));
  },

  /** The GPU load already recorded server-side, so the detail graph opens with a past
   *  instead of filling from empty after a reload. */
  async opsVitalsHistory(seconds: number): Promise<VitalsHistorySample[]> {
    const response = await request(`/api/ops/vitals/history?seconds=${seconds}`);
    return ((await response.json()) as { samples: VitalsHistorySample[] }).samples;
  },

  /** What the box was DOING across the window the graph is showing — model loads, the
   *  evictions that made room for them, image renders. The half of the vitals surface that
   *  explains a pinned GPU with an empty roster. */
  async opsVitalsEvents(seconds: number): Promise<BoxEvent[]> {
    const response = await request(`/api/ops/vitals/events?seconds=${seconds}`);
    return ((await response.json()) as { events: BoxEvent[] }).events;
  },

  /** One turn's step trail and raw output, for the vitals detail's level 2. Polled
   *  while that level is open so a streaming answer grows on screen. */
  async opsTurnDetail(runId: string): Promise<TurnDetail> {
    const response = await request(`/api/ops/turns/${encodeURIComponent(runId)}`);
    return (await response.json()) as TurnDetail;
  },

  // ===== The workflow run log — the Ops "Runs" surface (owner-only) =====

  // The run log, filtered server-side so the surface reaches past the recency window
  // (picking "Agent" fetches the last N agent turns from history, not just whatever
  // survived the reconcile noise in the recent 50).
  async runs(params: RunListParams = {}): Promise<RunSummary[]> {
    const q = new URLSearchParams();
    for (const kind of params.kinds ?? []) q.append("kinds", kind);
    if (params.excludeSweeps) q.set("exclude_sweeps", "true");
    if (params.since) q.set("since", params.since);
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    const qs = q.toString();
    const response = await request(`/api/runs${qs ? `?${qs}` : ""}`);
    return (await response.json()) as RunSummary[];
  },

  async run(id: string): Promise<RunDetail> {
    const response = await request(`/api/runs/${encodeURIComponent(id)}`);
    return (await response.json()) as RunDetail;
  },

  // The tile + chip-count aggregates (over the whole log, not the fetched page), so
  // the tiles stay honest while the list is filtered. `since`/`excludeSweeps` scope
  // the per-kind counts to match the active filters.
  async runsStats(params: { excludeSweeps?: boolean; since?: string } = {}): Promise<RunStats> {
    const q = new URLSearchParams();
    if (params.excludeSweeps) q.set("exclude_sweeps", "true");
    if (params.since) q.set("since", params.since);
    const qs = q.toString();
    const response = await request(`/api/runs/stats${qs ? `?${qs}` : ""}`);
    return (await response.json()) as RunStats;
  },

  // The job-queue backlog (status='queued' in app.jobs) for the "jobs queued" tile.
  async queueDepth(): Promise<number> {
    const response = await request("/api/runs/queue-depth");
    return ((await response.json()) as { queued: number }).queued;
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

  // ===== The Research Library (owner browse over jerv's external corpus) =====
  // Reports key on their uuid; videos key on video_id (delete resolves the row id
  // server-side). Search returns `degraded` when the embed container is down (keyword-only).

  async researchReports(limit = 50): Promise<ReportListResponse> {
    const response = await request(`/api/research-library/reports?limit=${limit}`);
    return (await response.json()) as ReportListResponse;
  },

  async searchResearchReports(query: string, limit = 30): Promise<ReportSearchResponse> {
    const q = new URLSearchParams({ q: query, limit: String(limit) });
    const response = await request(`/api/research-library/reports/search?${q}`);
    return (await response.json()) as ReportSearchResponse;
  },

  async researchReport(id: string): Promise<ReportDetail> {
    const response = await request(`/api/research-library/reports/${encodeURIComponent(id)}`);
    return (await response.json()) as ReportDetail;
  },

  async deleteResearchReport(id: string): Promise<void> {
    await request(`/api/research-library/reports/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  /** Keep a report forever — clear its expiry so the nightly sweep won't reap it. */
  async keepResearchReport(id: string): Promise<void> {
    await request(`/api/research-library/reports/${encodeURIComponent(id)}/keep`, {
      method: "POST",
    });
  },

  /** Set the owner-chosen display title, overriding the LLM-generated heading. */
  async renameResearchReport(id: string, title: string): Promise<void> {
    await request(
      `/api/research-library/reports/${encodeURIComponent(id)}`,
      jsonInit("PATCH", { title }),
    );
  },

  // ----- public report share links (owner mint/list/revoke; anyone reads at /share/<token>) -----

  /** Mint a public, revocable, no-expiry link to one report OR one folder (owner only). The
   * secret is returned ONCE. `library_warning` flags a report drawn from private notes. */
  async mintReportShare(target: {
    target_kind: "report" | "group";
    report_id?: string;
    group_id?: string;
  }): Promise<MintedShare> {
    const response = await request("/api/research-library/shares", jsonInit("POST", target));
    return (await response.json()) as MintedShare;
  },

  /** The owner's live share links — metadata + usage, no secrets. */
  async researchShares(): Promise<ShareLinkView[]> {
    return (await (await request("/api/research-library/shares")).json()) as ShareLinkView[];
  },

  /** Revoke a share link (owner only). A 404 (already gone) is swallowed by the reload. */
  async revokeReportShare(id: string): Promise<void> {
    await request(`/api/research-library/shares/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  /** PUBLIC: resolve a share token to its report or folder index. 404 (ApiError) when the
   * link is unknown or revoked. No login — used by the shell-less ResearchShareApp. */
  async researchShareView(token: string): Promise<ShareView> {
    return (await (
      await request(`/api/research-share/${encodeURIComponent(token)}`)
    ).json()) as ShareView;
  },

  /** PUBLIC: one report within a share (a folder member, or the report link's target). */
  async researchShareReport(token: string, reportId: string): Promise<ReportDetail> {
    const path = `/api/research-share/${encodeURIComponent(token)}/reports/${encodeURIComponent(reportId)}`;
    return (await (await request(path)).json()) as ReportDetail;
  },

  // ----- report folders (owner-named buckets on the Reports tab; mirrors task groups) -----

  async researchReportGroups(): Promise<ReportGroup[]> {
    const response = await request("/api/research-library/report-groups");
    return (await response.json()) as ReportGroup[];
  },

  async createReportGroup(name: string): Promise<ReportGroup> {
    const response = await request(
      "/api/research-library/report-groups",
      jsonInit("POST", { name }),
    );
    return (await response.json()) as ReportGroup;
  },

  async renameReportGroup(id: string, name: string): Promise<ReportGroup> {
    const response = await request(
      `/api/research-library/report-groups/${encodeURIComponent(id)}`,
      jsonInit("PATCH", { name }),
    );
    return (await response.json()) as ReportGroup;
  },

  async deleteReportGroup(id: string): Promise<void> {
    await request(`/api/research-library/report-groups/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  // File a report into a folder (null = the trailing "Ungrouped" section).
  async moveReport(reportId: string, groupId: string | null): Promise<void> {
    await request(
      `/api/research-library/reports/${encodeURIComponent(reportId)}/group`,
      jsonInit("PATCH", { group_id: groupId }),
    );
  },

  async researchVideos(limit = 50): Promise<VideoListResponse> {
    const response = await request(`/api/research-library/videos?limit=${limit}`);
    return (await response.json()) as VideoListResponse;
  },

  async searchResearchVideos(query: string, limit = 30): Promise<VideoSearchResponse> {
    const q = new URLSearchParams({ q: query, limit: String(limit) });
    const response = await request(`/api/research-library/videos/search?${q}`);
    return (await response.json()) as VideoSearchResponse;
  },

  async researchVideo(videoId: string): Promise<VideoDetail> {
    const response = await request(`/api/research-library/videos/${encodeURIComponent(videoId)}`);
    return (await response.json()) as VideoDetail;
  },

  async deleteResearchVideo(videoId: string): Promise<void> {
    await request(`/api/research-library/videos/${encodeURIComponent(videoId)}`, {
      method: "DELETE",
    });
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

  // Replace a schedule's timing spec (day/time/repeat, like a task). The server
  // recomputes next_run_at from the spec. Owner-only mutation.
  async updateSchedule(scheduleId: string, body: ScheduleInput): Promise<void> {
    await request(`/api/ops/schedules/${encodeURIComponent(scheduleId)}`, jsonInit("PUT", body));
  },

  // ===== Tasks (the launcher's Tasks surface) =====

  async tasks(): Promise<Task[]> {
    const response = await request("/api/tasks");
    return (await response.json()) as Task[];
  },

  async createTask(body: TaskInput): Promise<Task> {
    const response = await request("/api/tasks", jsonInit("POST", body));
    return (await response.json()) as Task;
  },

  async replaceTask(id: string, body: TaskInput): Promise<Task> {
    const response = await request(`/api/tasks/${encodeURIComponent(id)}`, jsonInit("PUT", body));
    return (await response.json()) as Task;
  },

  // The optimistic enable/disable toggle on a card.
  async setTaskEnabled(id: string, enabled: boolean): Promise<Task> {
    const response = await request(
      `/api/tasks/${encodeURIComponent(id)}`,
      jsonInit("PATCH", { enabled }),
    );
    return (await response.json()) as Task;
  },

  async deleteTask(id: string): Promise<void> {
    await request(`/api/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // Run a task now (synchronous on the server); returns the finished run.
  async runTask(id: string): Promise<TaskRun> {
    const response = await request(`/api/tasks/${encodeURIComponent(id)}/run`, { method: "POST" });
    return (await response.json()) as TaskRun;
  },

  async taskRuns(id: string): Promise<TaskRun[]> {
    const response = await request(`/api/tasks/${encodeURIComponent(id)}/runs`);
    return (await response.json()) as TaskRun[];
  },

  // ----- task groups (Direction B: chips + move sheet) -----

  async taskGroups(): Promise<TaskGroup[]> {
    const response = await request("/api/task-groups");
    return (await response.json()) as TaskGroup[];
  },

  async createTaskGroup(name: string): Promise<TaskGroup> {
    const response = await request("/api/task-groups", jsonInit("POST", { name }));
    return (await response.json()) as TaskGroup;
  },

  async renameTaskGroup(id: string, name: string): Promise<TaskGroup> {
    const response = await request(
      `/api/task-groups/${encodeURIComponent(id)}`,
      jsonInit("PATCH", { name }),
    );
    return (await response.json()) as TaskGroup;
  },

  async deleteTaskGroup(id: string): Promise<void> {
    await request(`/api/task-groups/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // Set the authoritative membership + order for one bucket's list. `groupId` null is
  // the Ungrouped bucket; `taskIds` is its full ordered id list (a drag reorders, a
  // "Move to…" appends the moved id to the destination). Returns the moved tasks.
  async reorderTasks(groupId: string | null, taskIds: string[]): Promise<Task[]> {
    const response = await request(
      "/api/tasks/reorder",
      jsonInit("POST", { group_id: groupId, task_ids: taskIds }),
    );
    return (await response.json()) as Task[];
  },

  // ===== Phase 4: the agent — sessions + Full Brain chat (docs/reference/ASSISTANT.md) =====

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
  async getChatCapabilities(): Promise<{
    supports_vision: boolean;
    can_analyze_images: boolean;
    context_window: number;
  }> {
    const response = await request("/api/chat/capabilities");
    return (await response.json()) as {
      supports_vision: boolean;
      can_analyze_images: boolean;
      context_window: number;
    };
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

  /** A session's currently-live turn (GET /api/chat/sessions/{id}/live-run), or null when it
   * has none. After a full PWA reload the in-memory run handle is gone, so this is how the
   * controller finds a still-running detached turn to reattach to: it seeds the bubble from
   * the returned `snapshot` (the turn's render so far — deep-research card, partial answer,
   * reasoning) and resumes the live SSE stream at `frameIndex` (chatResume), restoring the
   * bubble + sub-agent fan + Stop without a blank chat while the turn runs on. A 404 (no live
   * run) resolves to null rather than throwing. */
  async sessionLiveRun(sessionId: string): Promise<LiveRun | null> {
    try {
      const response = await request(
        `/api/chat/sessions/${encodeURIComponent(sessionId)}/live-run`,
      );
      const body = (await response.json()) as {
        run_id: string;
        snapshot: TranscriptTurn | null;
        frame_index: number;
      };
      return {
        runId: body.run_id,
        snapshot: body.snapshot ?? null,
        frameIndex: body.frame_index ?? 0,
      };
    } catch {
      return null;
    }
  },

  /** Cancel the in-flight chat turn (the composer's Stop). The turn runs detached
   * from the SSE stream server-side, so aborting the fetch no longer stops it — this
   * explicit signal does. Best-effort/idempotent on the server. */
  async cancelChatRun(runId: string): Promise<void> {
    await request(`/api/chat/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
  },

  /** Poll a deferred media analysis (the task_status card): its live progress, and — once
   * `status` is `done` — the finished result in the video_analysis card's data shape so
   * the card swaps to it (DEFERRED_TOOL_CALLS_PLAN.md P2/P3). */
  async deferredResult(resultId: string): Promise<DeferredResult> {
    const response = await request(`/api/chat/deferred/${encodeURIComponent(resultId)}`);
    return (await response.json()) as DeferredResult;
  },

  /** Stop a running deferred analysis (the card's Stop): cancels the worker job so its
   * ffmpeg/whisper legs terminate. Idempotent — a finished analysis is a no-op. */
  async cancelDeferredResult(resultId: string): Promise<void> {
    await request(`/api/chat/deferred/${encodeURIComponent(resultId)}/cancel`, { method: "POST" });
  },

  /** Claim a finished analysis' one auto-resume turn (the task_status card fires this the
   * moment it sees `done`). Atomic and exactly-once server-side: returns `true` only for
   * the first caller, so a job that finished while the card wasn't watching still resumes
   * on reopen, and reloads/extra tabs never double-prompt (DEFERRED_TOOL_CALLS_PLAN P3). */
  async claimDeferredResult(resultId: string): Promise<boolean> {
    const response = await request(`/api/chat/deferred/${encodeURIComponent(resultId)}/claim`, {
      method: "POST",
    });
    return ((await response.json()) as { claimed: boolean }).claimed;
  },

  // --- Code mode (jcode), Wave J3. Owner-only; routes 404 when jcode isn't enabled. ---

  /** The launcher's session index (owner-only `jcode_sessions`). */
  async jcodeSessions(): Promise<JcodeSession[]> {
    return (await request("/api/jcode/sessions")).json();
  },

  /** One session — reachable by the owner OR a redeemed share scoped to it (the launcher
   * list is owner-only, so a share opens straight to the session via this). */
  async jcodeGetSession(id: string): Promise<JcodeSession> {
    return (await request(`/api/jcode/sessions/${encodeURIComponent(id)}`)).json();
  },

  /** Mint a copy-link for this session (owner only): a scoped, expiring, revocable
   * secret. Returned ONCE — build the share URL from it; it can't be re-read. */
  async jcodeMintShare(id: string, ttlHours = 24): Promise<JcodeShareToken> {
    return (
      await request(
        `/api/jcode/sessions/${encodeURIComponent(id)}/share`,
        jsonInit("POST", { ttl_hours: ttlHours }),
      )
    ).json();
  },

  /** The live (non-revoked) share links for a session (owner only) — metadata only. */
  async jcodeListShares(id: string): Promise<JcodeShare[]> {
    return (await request(`/api/jcode/sessions/${encodeURIComponent(id)}/shares`)).json();
  },

  /** Revoke a share link (owner only). Idempotent from the UI's view — a 404 (already
   * gone) is swallowed by the caller's reload. */
  async jcodeRevokeShare(id: string, shareId: string): Promise<void> {
    await request(
      `/api/jcode/sessions/${encodeURIComponent(id)}/shares/${encodeURIComponent(shareId)}`,
      { method: "DELETE" },
    );
  },

  /** Redeem a share secret on any browser: sets a session cookie scoped to that one
   * session and returns its id. 401 (ApiError) on an invalid / expired / revoked /
   * already-claimed link (share links are single-use — first browser binds it). */
  async jcodeRedeemShare(token: string): Promise<{ session_id: string }> {
    return (await request("/api/jcode/share/redeem", jsonInit("POST", { token }))).json();
  },

  // --- Math launcher (jlaunch). Owner-only; routes 404 when the launcher isn't enabled. ---

  /** The registered job specs (also the launcher-tile enablement probe: 404 = disabled). */
  async jlaunchSpecs(): Promise<JlaunchSpec[]> {
    return (await request("/api/jlaunch/specs")).json();
  },

  /** All jobs (live + finished), reconciled against the durable run mirror. */
  async jlaunchJobs(): Promise<JlaunchJob[]> {
    return (await request("/api/jlaunch/jobs")).json();
  },

  async jlaunchGetJob(id: string): Promise<JlaunchJob> {
    return (await request(`/api/jlaunch/jobs/${encodeURIComponent(id)}`)).json();
  },

  /** Start a job for a spec. 409 (ApiError) if one is already running. */
  async jlaunchStart(spec: string): Promise<JlaunchJob> {
    return (await request("/api/jlaunch/jobs", jsonInit("POST", { spec }))).json();
  },

  /** Graceful stop (SIGTERM) — the job finalizes as `killed`. */
  async jlaunchStop(id: string): Promise<JlaunchJob> {
    return (
      await request(`/api/jlaunch/jobs/${encodeURIComponent(id)}/stop`, { method: "POST" })
    ).json();
  },

  /** Hard kill (SIGKILL + process sweep) — the job finalizes as `killed`. */
  async jlaunchKill(id: string): Promise<JlaunchJob> {
    return (
      await request(`/api/jlaunch/jobs/${encodeURIComponent(id)}/kill`, { method: "POST" })
    ).json();
  },

  /** Delete a job: kills it if live, then removes the checkout, scratch, and mirror row. */
  async jlaunchDelete(id: string): Promise<void> {
    await request(`/api/jlaunch/jobs/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  /** Mint a public results-share link for a finished, artifact-bearing job (owner only).
   * Streams the artifact into the blob store, then returns the bearer secret ONCE. */
  async jlaunchMintShare(id: string, label = "shared results"): Promise<JlaunchShareToken> {
    return (
      await request(
        `/api/jlaunch/jobs/${encodeURIComponent(id)}/share`,
        jsonInit("POST", { label }),
      )
    ).json();
  },

  /** The live (non-revoked) share links for a job (owner only) — metadata only. */
  async jlaunchListShares(id: string): Promise<JlaunchShare[]> {
    return (await request(`/api/jlaunch/jobs/${encodeURIComponent(id)}/shares`)).json();
  },

  /** Revoke a results-share link (owner only). */
  async jlaunchRevokeShare(id: string, shareId: string): Promise<void> {
    await request(
      `/api/jlaunch/jobs/${encodeURIComponent(id)}/shares/${encodeURIComponent(shareId)}`,
      { method: "DELETE" },
    );
  },

  /** PUBLIC: resolve a results-share token to its run. 404 (ApiError) when unknown/revoked. */
  async jlaunchShareView(token: string): Promise<PublicJlaunchRun> {
    return (await request(`/api/jlaunch-share/${encodeURIComponent(token)}`)).json();
  },

  /** Guided intake (recipient). Redeem a share secret for a session-scoped cookie + the
   * link's config. A 401 means an invalid / expired / exhausted link. */
  async intakeRedeem(secret: string): Promise<IntakeConfig> {
    return (await request("/api/intake/redeem", jsonInit("POST", { secret }))).json();
  },

  /** One interview turn, streamed as SSE (same framing as `chat`). The intake persona
   * has no tools, so the stream is text_delta + usage + done only. The cookie scopes it
   * to the recipient's own session — no session id in the body. */
  async *intakeChat(message: string, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
    const response = await request("/api/intake/chat", {
      ...jsonInit("POST", { message }),
      ...(signal ? { signal } : {}),
    });
    if (!response.body) return;
    yield* parseChatStream(response.body);
  },

  /** Confirm the draft → capture the submission for the owner to review. */
  async intakeConfirm(entererName: string): Promise<IntakeConfirmOut> {
    return (
      await request("/api/intake/confirm", jsonInit("POST", { enterer_name: entererName }))
    ).json();
  },

  // --- Guided intake (owner management, W6) — every route is owner-gated. ---

  /** Every minted link, newest first — metadata only, never a secret. */
  async listIntakeLinks(): Promise<IntakeLink[]> {
    return (await request("/api/intake/links")).json();
  },

  async getIntakeLink(id: string): Promise<IntakeLink> {
    return (await request(`/api/intake/links/${encodeURIComponent(id)}`)).json();
  },

  /** Revoke a link — it and its open sessions stop working. */
  async revokeIntakeLink(id: string): Promise<void> {
    await request(`/api/intake/links/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  /** Mint a link directly (the re-mint path clones an existing link's config to a
   * fresh show-once secret). Returns the secret exactly once. */
  async mintIntakeLink(body: IntakeMintRequest): Promise<IntakeMintResult> {
    return (await request("/api/intake/links", jsonInit("POST", body))).json();
  },

  /** The link's opened sessions (the owner's conversation browse). */
  async listIntakeSessions(linkId: string): Promise<IntakeSessionRow[]> {
    return (await request(`/api/intake/links/${encodeURIComponent(linkId)}/sessions`)).json();
  },

  /** The link's captured submissions, newest first (transcripts read separately). */
  async listIntakeSubmissions(linkId: string): Promise<IntakeSubmission[]> {
    return (await request(`/api/intake/links/${encodeURIComponent(linkId)}/submissions`)).json();
  },

  /** One submission with its full transcript (the read-only conversation view). */
  async getIntakeSubmission(submissionId: string): Promise<IntakeSubmissionDetail> {
    return (await request(`/api/intake/submissions/${encodeURIComponent(submissionId)}`)).json();
  },

  /** Materialize a captured submission into an owner Proposal for the review inbox.
   * Returns the staged proposal's id. */
  async materializeIntakeSubmission(submissionId: string): Promise<{ proposal_id: string }> {
    return (
      await request(`/api/intake/submissions/${encodeURIComponent(submissionId)}/materialize`, {
        method: "POST",
      })
    ).json();
  },

  /** Edit a staged intake-link Proposal's config before approval (the editable-Proposal
   * surface). Only the soft fields — subject/domain are fixed and re-validated at mint. */
  async patchIntakeProposalConfig(nodeId: string, patch: IntakeConfigPatch): Promise<void> {
    await request(
      `/api/intake/proposals/nodes/${encodeURIComponent(nodeId)}/config`,
      jsonInit("PATCH", patch),
    );
  },

  /** Approve → mint the link from a staged intake-link Proposal: re-validates the
   * subject/domain, mints show-once, marks the Proposal enacted. Secret shown once. */
  async mintIntakeLinkFromProposal(proposalId: string): Promise<IntakeMintResult> {
    return (
      await request(`/api/intake/links/from-proposal/${encodeURIComponent(proposalId)}`, {
        method: "POST",
      })
    ).json();
  },

  /** External-LLM sessions (owner only): a token-gated public endpoint exposing the
   * on-box coder to a remote Claude. List metadata + cumulative usage. */
  async externalSessions(): Promise<ExternalSession[]> {
    return (await request("/api/jcode/external")).json();
  },

  /** Mint an external session; returns the bearer secret + endpoint URL ONCE. */
  async externalMint(label: string, ttlHours?: number): Promise<ExternalMint> {
    const body: { label: string; ttl_hours?: number } = { label };
    if (ttlHours) body.ttl_hours = ttlHours;
    return (await request("/api/jcode/external", jsonInit("POST", body))).json();
  },

  /** Flip an external session's on/off toggle. */
  async externalSetEnabled(id: string, enabled: boolean): Promise<void> {
    await request(
      `/api/jcode/external/${encodeURIComponent(id)}/enabled`,
      jsonInit("POST", { enabled }),
    );
  },

  /** Revoke (delete) an external session. */
  async externalRevoke(id: string): Promise<void> {
    await request(`/api/jcode/external/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  /** Spin a new sandboxed session (clone a repo or scratch). */
  async jcodeCreateSession(body: NewSessionInput): Promise<JcodeSession> {
    return (await request("/api/jcode/sessions", jsonInit("POST", body))).json();
  },

  async jcodeResetSession(id: string): Promise<JcodeSession> {
    return (
      await request(`/api/jcode/sessions/${encodeURIComponent(id)}/reset`, { method: "POST" })
    ).json();
  },

  async jcodeDeleteSession(id: string): Promise<void> {
    await request(`/api/jcode/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  /** Pause a session: the sandbox kills its processes but keeps the checkout, so it can
   * be restarted. The shell-exit path does this server-side; this is the explicit call. */
  async jcodeStopSession(id: string): Promise<JcodeSession> {
    return (
      await request(`/api/jcode/sessions/${encodeURIComponent(id)}/stop`, { method: "POST" })
    ).json();
  },

  /** Resume a paused session (its checkout is still on disk). */
  async jcodeRestartSession(id: string): Promise<JcodeSession> {
    return (
      await request(`/api/jcode/sessions/${encodeURIComponent(id)}/restart`, { method: "POST" })
    ).json();
  },

  /** Rename a session (the launcher's swipe rail). "" clears the label back to the repo. */
  async jcodeRenameSession(id: string, title: string): Promise<void> {
    await request(`/api/jcode/sessions/${encodeURIComponent(id)}`, jsonInit("PATCH", { title }));
  },

  /** Tidy a session out of the live list without deleting it (launcher rail). */
  async jcodeArchiveSession(id: string): Promise<void> {
    await request(`/api/jcode/sessions/${encodeURIComponent(id)}/archive`, { method: "POST" });
  },

  /** Restore an archived session to the live list (launcher rail). */
  async jcodeUnarchiveSession(id: string): Promise<void> {
    await request(`/api/jcode/sessions/${encodeURIComponent(id)}/unarchive`, { method: "POST" });
  },

  /** Per-session web preview (Wave J4): an ephemeral tunnel to the sandbox dev server. */
  async jcodePreviewStatus(id: string): Promise<JcodePreview> {
    return (await request(`/api/jcode/sessions/${encodeURIComponent(id)}/preview`)).json();
  },

  async jcodePreviewOpen(id: string, port?: number): Promise<JcodePreview> {
    return (
      await request(
        `/api/jcode/sessions/${encodeURIComponent(id)}/preview`,
        jsonInit("POST", { port: port ?? null }),
      )
    ).json();
  },

  /** Whether the coder model is resident in the gateway — polled by the loading bar. */
  async jcodeModelStatus(): Promise<JcodeModelStatus> {
    return (await request("/api/jcode/model")).json();
  },

  /** Explicitly warm the coder onto the box (evicts the other resident models). Called
   * after the owner confirms the swap; returns the fresh status. */
  async jcodeWarmModel(): Promise<JcodeModelStatus> {
    return (await request("/api/jcode/model/warm", { method: "POST" })).json();
  },

  /** The master on/off state for code mode (services up + coder), for the launcher switch. */
  async jcodePower(): Promise<JcodePowerStatus> {
    return (await request("/api/jcode/power")).json();
  },

  /** Bring the jcode-only services up (on) or down (off); returns the fresh power state.
   * Powering on only starts the services — the caller then warms the coder separately. */
  async jcodeSetPower(on: boolean): Promise<JcodePowerStatus> {
    return (await request("/api/jcode/power", jsonInit("POST", { on }))).json();
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

  async decideNode(
    proposalId: string,
    nodeId: string,
    decision: Decision,
    reason?: string,
  ): Promise<void> {
    await request(
      `/api/proposals/${encodeURIComponent(proposalId)}/nodes/${encodeURIComponent(nodeId)}/decision`,
      jsonInit("POST", reason ? { decision, reason } : { decision }),
    );
  },

  /** Correct-in-place: replace a staged note/appointment leaf's proposed body before
   * approval. The edit files as the owner's correction at enact (provenance='human'). */
  async editNode(proposalId: string, nodeId: string, body: string): Promise<void> {
    await request(
      `/api/proposals/${encodeURIComponent(proposalId)}/nodes/${encodeURIComponent(nodeId)}/edit`,
      jsonInit("POST", { body }),
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

  // --- Image launcher (Wave L1). Newest-first owner-only gallery + the direct,
  // non-agent generate/edit calls. Mock-backed until the L3 render API lands.

  async listGeneratedImages(): Promise<GeneratedImageOut[]> {
    const response = await request("/api/images/generated");
    return (await response.json()) as GeneratedImageOut[];
  },

  async generateImage(req: GenerateImageRequest): Promise<GeneratedImageOut> {
    const response = await request("/api/images/generate", jsonInit("POST", req));
    return (await response.json()) as GeneratedImageOut;
  },

  // The source/references are bytes, so this is multipart: the spec rides a JSON
  // part, an uploaded source (when there's no `sourceImageId`) and up to 2
  // references ride file parts the L3 endpoint sniffs + size-caps.
  async editImage(
    req: EditImageRequest,
    source?: File | null,
    refs?: File[],
  ): Promise<GeneratedImageOut> {
    const form = new FormData();
    form.append("spec", JSON.stringify(req));
    if (source) form.append("source", source, source.name);
    for (const ref of refs ?? []) form.append("references", ref, ref.name);
    const response = await request("/api/images/edit", { method: "POST", body: form });
    return (await response.json()) as GeneratedImageOut;
  },

  // Row-only: the owner-only row goes; the blob is content-addressed/keep-all
  // (possibly shared by another render or an edit's source), so it stays.
  async deleteGeneratedImage(id: string): Promise<void> {
    await request(`/api/images/generated/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // ----- JPet: the wall pet, one server-authoritative state both surfaces render -----
  async getPet(): Promise<PetState> {
    const response = await request("/api/pet");
    return (await response.json()) as PetState;
  },
  async sendPetCommand(command: PetCommand): Promise<PetState> {
    const response = await request("/api/pet/command", jsonInit("POST", command));
    return (await response.json()) as PetState;
  },
  // Subscribe to the live pet stream (SSE, `data: <json>\n\n` frames, like /api/chat):
  // an initial snapshot then every change. Iterate under an AbortController; the
  // caller's useEffect cleanup aborts to unsubscribe.
  async *petStream(signal?: AbortSignal): AsyncGenerator<PetState> {
    const response = await request("/api/pet/stream", signal ? { signal } : {});
    if (!response.body) return;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const line = frame.split("\n").find((l) => l.startsWith("data:"));
          const jsonText = line?.slice("data:".length).trim();
          if (jsonText) {
            try {
              yield JSON.parse(jsonText) as PetState;
            } catch {
              // skip a malformed frame; the next snapshot supersedes it
            }
          }
          boundary = buffer.indexOf("\n\n");
        }
      }
    } finally {
      reader.releaseLock();
    }
  },
};
