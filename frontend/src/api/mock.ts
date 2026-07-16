// In-memory fixture backend for `npm run dev:mock` (VITE_MOCK=1).
// Mirrors the real API contract closely enough for UI work: idempotent
// note creation, cursor pagination, multipart attachments, always-on auth.

import type {
  AppSettings,
  AttachmentExtract,
  AttachmentOut,
  ContainerStatus,
  DeviceSummary,
  EgoGraph,
  EntityListItem,
  EntityOut,
  FactOut,
  GeneratedImageOut,
  GraphEdge,
  ImageSettings,
  LlmProviderId,
  LlmSettings,
  LlmUsage,
  NoteAnalysis,
  NoteOut,
  PetCommand,
  PetState,
  Principal,
  ReasoningEffort,
  ReviewItem,
  RunDetail,
  RunSummary,
  SearchHit,
  SearchMatch,
  SearchResult,
  SweepTrigger,
  WikiArticleOut,
  WikiLandingOut,
  WikiSearchResult,
  WikiTalkOut,
  WikiTalkTopic,
} from "./client";

const PRINCIPAL: Principal = {
  principal_id: "mock-owner",
  kind: "owner_device",
  label: "Mock device",
};

const mockUpdate = { state: "none" as "none" | "running" | "exited", ticks: 0 };
const mockRebuild = { state: "none" as "none" | "running" | "exited", ticks: 0 };
const mockExport = { state: "none" as "none" | "running" | "exited", ticks: 0 };
const mockImport = { state: "none" as "none" | "running" | "exited", ticks: 0 };
const mockReset = { state: "none" as "none" | "running" | "exited", ticks: 0 };

const CONTAINERS: ContainerStatus[] = [
  {
    service: "api",
    state: "running",
    health: "healthy",
    started_at: new Date(Date.now() - 36e5 * 5).toISOString(),
    image: "jbrain/api:edge",
  },
  {
    service: "postgres",
    state: "running",
    health: "healthy",
    started_at: new Date(Date.now() - 36e5 * 30).toISOString(),
    image: "postgres:17",
  },
  {
    service: "worker",
    state: "exited",
    health: null,
    started_at: null,
    image: "jbrain/worker:edge",
  },
  {
    service: "supervisor",
    state: "running",
    health: "healthy",
    started_at: new Date(Date.now() - 36e5 * 5).toISOString(),
    image: "jbrain/supervisor:edge",
  },
  {
    service: "local-llm",
    state: "running",
    health: null,
    started_at: new Date(Date.now() - 36e5 * 2).toISOString(),
    image: "jbrain/local-llm:edge",
  },
  {
    service: "embed",
    state: "running",
    health: null,
    started_at: new Date(Date.now() - 36e5 * 6).toISOString(),
    image: "ghcr.io/huggingface/text-embeddings-inference",
  },
  {
    service: "proxy",
    state: "running",
    health: null,
    started_at: new Date(Date.now() - 36e5 * 5).toISOString(),
    image: "jbrain/proxy:edge",
  },
  {
    service: "searxng",
    state: "running",
    health: null,
    started_at: new Date(Date.now() - 36e5 * 6).toISOString(),
    image: "docker.io/searxng/searxng:latest",
  },
  {
    service: "cloudflared",
    state: "running",
    health: null,
    started_at: new Date(Date.now() - 36e5 * 6).toISOString(),
    image: "cloudflare/cloudflared:latest",
  },
];

// The Ops "Runs" surface (Direction C) fixtures: a running integration run, a
// running pipeline sweep, a failed run with a step error, and finished runs.
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
const MOCK_RUN_DETAILS: RunDetail[] = [
  {
    id: "run-r1",
    kind: "integration",
    status: "running",
    name: "integrate_note",
    started_at: ago(12_000),
    duration_ms: null,
    step_count: 5,
    cost_tokens: 4100,
    stop_reason: null,
    progress_note: "processed 3 of 5 steps",
    steps: [
      {
        idx: 0,
        kind: "model",
        name: "classify domain",
        ok: true,
        cost_tokens: 300,
        job_id: null,
        error: null,
      },
      {
        idx: 1,
        kind: "tool",
        name: "entity_resolve",
        ok: true,
        cost_tokens: 1200,
        job_id: null,
        error: null,
      },
      {
        idx: 2,
        kind: "model",
        name: "extract facts",
        ok: true,
        cost_tokens: 2600,
        job_id: null,
        error: null,
      },
    ],
  },
  {
    id: "run-r2",
    kind: "pipeline",
    status: "running",
    name: "consolidate_predicates",
    started_at: ago(124_000),
    duration_ms: null,
    step_count: 2,
    cost_tokens: 800,
    stop_reason: null,
    progress_note: null,
    steps: [
      {
        idx: 0,
        kind: "job",
        name: "consolidate_predicates",
        ok: true,
        cost_tokens: 800,
        job_id: "job-1",
        error: null,
      },
    ],
  },
  {
    // A manually-fired pipeline whose steps are still waiting behind the
    // single-threaded worker — derived 'queued', not yet running.
    id: "run-rq",
    kind: "pipeline",
    status: "queued",
    name: "daily_inbox_triage",
    started_at: ago(4_000),
    duration_ms: null,
    step_count: 1,
    cost_tokens: 0,
    stop_reason: null,
    progress_note: null,
    steps: [
      {
        idx: 0,
        kind: "job",
        name: "daily_inbox_triage",
        ok: true,
        cost_tokens: 0,
        job_id: "job-q1",
        error: null,
      },
    ],
  },
  {
    id: "run-r3",
    kind: "integration",
    status: "error",
    name: "integrate_note",
    started_at: ago(1_080_000),
    duration_ms: 31_000,
    step_count: 3,
    cost_tokens: 6700,
    stop_reason: "step_error",
    progress_note: null,
    steps: [
      {
        idx: 0,
        kind: "model",
        name: "classify domain",
        ok: true,
        cost_tokens: 300,
        job_id: null,
        error: null,
      },
      {
        idx: 1,
        kind: "job",
        name: "ocr_attachment · labs.pdf",
        ok: false,
        cost_tokens: 1100,
        job_id: "job-7",
        error:
          "TimeoutError: vision adapter timeout after 30s (attempt 3/3) — marked PermanentJobError. Downstream extract skipped.",
      },
    ],
  },
  {
    id: "run-r4",
    kind: "agent",
    status: "done",
    name: "agent",
    started_at: ago(3_600_000),
    duration_ms: 48_000,
    step_count: 4,
    cost_tokens: 21_400,
    stop_reason: "end_turn",
    progress_note: null,
    steps: [
      {
        idx: 0,
        kind: "model",
        name: "plan turn",
        ok: true,
        cost_tokens: 3100,
        job_id: null,
        error: null,
      },
      {
        idx: 1,
        kind: "tool",
        name: "search_notes",
        ok: true,
        cost_tokens: 1000,
        job_id: null,
        error: null,
      },
      {
        idx: 2,
        kind: "model",
        name: "draft proposal",
        ok: true,
        cost_tokens: 8200,
        job_id: null,
        error: null,
      },
    ],
  },
  // The scheduler's high-frequency, ~0-token housekeeping sweeps — they fire every
  // few minutes and bury the runs that carry signal. Included so mock mode exercises
  // the Runs filter's "hide reconcile sweeps" / kind toggles (docs/mocks/runs-filter).
  ...(
    [
      ["reconcile_pending_integration", 60_000],
      ["reconcile_unembedded_notes", 63_000],
      ["reconcile_pending_notes", 66_000],
      ["geofence_sweep", 120_000],
      ["reconcile_pending_integration", 360_000],
      ["reconcile_pending_notes", 366_000],
    ] as const
  ).map(([name, msAgo], i) => ({
    id: `run-sweep-${i}`,
    kind: "pipeline",
    status: "done" as const,
    name,
    started_at: ago(msAgo),
    duration_ms: 100 + i,
    step_count: 1,
    cost_tokens: 0,
    stop_reason: "end" as const,
    progress_note: null,
    steps: [
      {
        idx: 0,
        kind: "job",
        name,
        ok: true,
        cost_tokens: 0,
        job_id: `job-sweep-${i}`,
        error: null,
      },
    ],
  })),
];

const MOCK_RUNS: RunSummary[] = MOCK_RUN_DETAILS.map(
  ({
    id,
    kind,
    status,
    name,
    started_at,
    duration_ms,
    step_count,
    cost_tokens,
    progress_note,
    steps,
  }) => ({
    id,
    kind,
    status,
    name,
    started_at,
    duration_ms,
    step_count,
    cost_tokens,
    last_error: status === "error" ? (steps.find((s) => !s.ok)?.name ?? null) : null,
    progress_note,
  }),
);

const MOCK_SWEEPS: SweepTrigger[] = [
  { id: "sweep-1", pipeline: "consolidate_predicates", label: "Consolidate" },
  { id: "sweep-2", pipeline: "sync_predicates", label: "Sync predicates" },
];

let nextId = 1;
function id(prefix: string): string {
  return `${prefix}-${nextId++}`;
}

const attachmentBlobs = new Map<string, Blob>();
// Vision-cache fixtures for the manifest expansion, keyed by attachment id.
const attachmentExtracts = new Map<string, AttachmentExtract[]>();
// Attachments with an on-demand analyze in flight (409s a second POST).
const analyzingAttachments = new Set<string>();
// Notes with a note-level re-run in flight (409s a second POST).
const analyzingNotes = new Set<string>();

// The first server-synced settings object (theme/text-size stay local).
const SETTINGS: AppSettings = {
  image_analysis_mode: "full",
  owner_timezone: null,
  brain_llm_stream: false,
  brain_read_aloud: false,
  brain_answer_voice: "en_US-amy-medium",
  brain_read_aloud_engine: "piper",
};

// The box's installed piper voices, for the read-aloud voice picker mock — one curated
// multi-speaker entry (libritts_r 3922) alongside the two single-speaker defaults.
const BRAIN_VOICES = ["en_US-amy-medium", "en_US-joe-medium", "en_US-libritts_r-medium#3922"];

// The multi-speaker libritts_r roster the Settings voice explorer shuffles across — speaker
// names ordered by piper index (index 0 is the curated female speaker 3922). A synthetic
// 902-entry list in dev; the real box reads the names from the model's speaker_id_map.
const BRAIN_SPEAKERS: Record<string, string[]> = {
  "en_US-libritts_r-medium": ["3922", ...Array.from({ length: 901 }, (_, i) => String(1000 + i))],
};

// Per-task LLM routing fixture (GET/PUT /api/settings/llm). Only grok carries
// a reasoning level; reasoning_effort is null for any task off grok, mirroring
// the wire contract the screen relies on for hiding the reasoning control.
const LLM_REASONING_DEFAULT: ReasoningEffort = "low";
const LLM_SETTINGS: LlmSettings = {
  providers: [
    { id: "grok", label: "Grok 4.3", supports_reasoning: true, supports_vision: true },
    { id: "claude", label: "Claude Sonnet 4.6", supports_reasoning: false, supports_vision: true },
    { id: "local", label: "Local model", supports_reasoning: false, supports_vision: true },
  ],
  reasoning_efforts: ["none", "low", "medium", "high"],
  reasoning_default: LLM_REASONING_DEFAULT,
  tasks: [
    { id: "agent.turn", label: "Agent turn", provider: "grok", reasoning_effort: "medium" },
    {
      id: "agent.vision",
      label: "Agent image analysis",
      provider: "grok",
      reasoning_effort: null,
    },
    { id: "integrate.note", label: "Integrate note", provider: "grok", reasoning_effort: "medium" },
    { id: "fact.adjudicate", label: "Fact adjudicate", provider: "grok", reasoning_effort: "high" },
    {
      id: "entity.disambiguate",
      label: "Entity disambiguate",
      provider: "grok",
      reasoning_effort: "medium",
    },
    { id: "note.extract", label: "Note extract", provider: "grok", reasoning_effort: "low" },
    {
      id: "correction_note.extract",
      label: "Correction extract",
      provider: "grok",
      reasoning_effort: "low",
    },
    { id: "session.title", label: "Session title", provider: "claude", reasoning_effort: null },
    { id: "vision.ocr", label: "Vision OCR", provider: "local", reasoning_effort: null },
    { id: "vision.caption", label: "Vision caption", provider: "grok", reasoning_effort: "low" },
  ],
  local_hosting_enabled: true,
  // A non-authoritative snapshot of jbrain.llm.local_catalog for the offline UI
  // mock only — the backend is the source of truth; this may drift. One model is
  // provisioned (enabled) so the redesign's Installed/Catalog tabs + the Uninstall
  // control are exercisable in `dev:mock`.
  local_models: [
    {
      id: "qwen3-vl-30b",
      label: "Qwen3-VL 30B · vision",
      enabled: true,
      queued: false,
      remove_queued: false,
      loaded: false,
      supports_vision: true,
      supports_tools: true,
      tiers: ["vision", "low"],
      quant: "Q8_0",
      size_gb: 32,
      disk_gb: 31.7,
      download_gb: null,
      note: "Vision + a capable cheap text model.",
      context_window: 32768,
      max_context_window: 262144,
      context_window_override: null,
      staged: false,
      kv_gb: 1.5,
    },
    {
      id: "gpt-oss-120b",
      label: "GPT-OSS 120B · reasoning",
      enabled: false,
      queued: false,
      remove_queued: false,
      loaded: false,
      supports_vision: false,
      supports_tools: true,
      tiers: ["high"],
      quant: "MXFP4",
      size_gb: 59,
      disk_gb: null,
      download_gb: null,
      note: "Strongest open reasoning that still runs fast here.",
      context_window: 131072,
      max_context_window: 131072,
      context_window_override: null,
      staged: false,
      kv_gb: 4.5,
    },
    {
      id: "qwen3-235b-a22b",
      label: "Qwen3-235B-A22B · reasoning (alt, 3-bit)",
      enabled: false,
      queued: false,
      remove_queued: false,
      loaded: false,
      supports_vision: false,
      supports_tools: true,
      tiers: ["high"],
      quant: "UD-Q3_K_XL",
      size_gb: 104.2,
      disk_gb: null,
      download_gb: null,
      note: "235B MoE, 22B active — the strongest open model that fits this 128 GB box, at 3-bit. Standalone only (swappable).",
      context_window: 32768,
      max_context_window: 262144,
      context_window_override: null,
      staged: false,
      kv_gb: 11.5,
    },
    {
      id: "qwen3.5-122b-a10b",
      label: "Qwen3.5 122B · vision + reasoning",
      enabled: false,
      queued: false,
      remove_queued: false,
      loaded: false,
      supports_vision: true,
      supports_tools: true,
      tiers: ["vision", "high"],
      quant: "UD-Q4_K_XL",
      size_gb: 72.6,
      disk_gb: null,
      download_gb: null,
      note: "122B MoE, 10B active — Qwen3.5's flagship at 4-bit. The strongest vision model here and a credible high-tier reasoner. Standalone (swappable).",
      context_window: 32768,
      max_context_window: 262144,
      context_window_override: null,
      staged: false,
      kv_gb: 7,
    },
    {
      id: "qwen3.5-122b-a10b-mtp",
      label: "Qwen3.5 122B · vision + reasoning (MTP, faster)",
      enabled: false,
      queued: false,
      remove_queued: false,
      loaded: false,
      supports_vision: true,
      supports_tools: true,
      tiers: ["vision", "high"],
      quant: "UD-Q4_K_XL",
      size_gb: 74,
      disk_gb: null,
      download_gb: null,
      note: "122B MoE, 10B active — the multi-token-prediction build: same model + vision tower + a draft head for ~1.5–2x faster self-speculative decoding. Standalone (swappable).",
      context_window: 32768,
      max_context_window: 131072,
      context_window_override: null,
      staged: false,
      kv_gb: 7,
    },
  ],
  host_memory: null,
  jcode: {
    enabled: true,
    model: "qwen3-coder-next",
    default: "qwen3-coder-next",
    planner: "gpt-oss-120b",
    planner_default: "gpt-oss-120b",
    planner_same: "same",
    options: [
      { id: "qwen3-coder-next", label: "Qwen3-Coder-Next 80B · coding agent" },
      { id: "qwen3-vl-30b", label: "Qwen3-VL 30B · vision" },
      { id: "gpt-oss-120b", label: "GPT-OSS 120B · reasoning" },
    ],
  },
};

// The on-box image service for the drawer's image subsection + shared meter. On,
// reachable, with a generate model resident (VRAM used = 128 - 96) so the unified
// bar shows an image segment beside the LLMs.
const IMAGE_SETTINGS: ImageSettings = {
  enabled: true,
  reachable: true,
  models: [
    {
      id: "qwen-image",
      label: "Qwen-Image · generate (fp8)",
      kind: "generate",
      enabled: true,
      recommended: true,
      size_gb: 28,
      disk_gb: 27.3,
      vram_gb: 20,
      note: "Validated on Strix Halo: 1328x1328, 20 steps, ~3.5 min on the iGPU.",
    },
    {
      id: "qwen-image-edit",
      label: "Qwen-Image-Edit · edit",
      kind: "edit",
      enabled: false,
      recommended: false,
      size_gb: 44,
      disk_gb: null,
      vram_gb: 38,
      note: "Graph validated; bf16 weights await an on-box download.",
    },
  ],
  memory: { total_gb: 128, free_gb: 96 },
};

// Apply one task patch like the backend would: grok keeps/sets a reasoning
// level (defaulting when absent); any other provider nulls it out.
function applyLlmPatch(
  taskId: string,
  patch: { provider: LlmProviderId; reasoning_effort?: ReasoningEffort },
): void {
  const task = LLM_SETTINGS.tasks.find((t) => t.id === taskId);
  if (!task) return;
  task.provider = patch.provider;
  task.reasoning_effort =
    patch.provider === "grok"
      ? (patch.reasoning_effort ?? task.reasoning_effort ?? LLM_REASONING_DEFAULT)
      : null;
}

function makeAttachment(
  filename: string,
  mediaType: string,
  hasExtracts = false,
  hasDescription = false,
): AttachmentOut {
  const att = {
    id: id("att"),
    filename,
    media_type: mediaType,
    size_bytes: 24_120,
    has_extracts: hasExtracts,
    has_description: hasDescription,
  };
  attachmentBlobs.set(att.id, new Blob([`mock contents of ${filename}`], { type: mediaType }));
  return att;
}

const MOCK_DESCRIPTION =
  "A printed contractor quote on a kitchen counter, with a handwritten note " +
  "in the margin and a coffee mug holding the corner down.";

function extractFixtures(): AttachmentExtract[] {
  return [
    {
      kind: "ocr",
      text:
        "RIDGELINE ROOFING — QUOTE\n" +
        "1204 Pearl St, Boulder CO\n" +
        "tear-off + re-shingle        3,400\n" +
        "flashing (chimney + valley)    480\n" +
        "[illegible] disposal           320\n" +
        "--------------------------------\n" +
        "TOTAL                        4,200\n" +
        "valid 30 days — ask for Manny",
      tool: "xai:grok-4.3",
      confidence: 0.7,
      created_at: daysAgo(1, 10, 30),
      words: null,
    },
    {
      kind: "caption",
      text: MOCK_DESCRIPTION,
      tool: "xai:grok-4.3",
      confidence: 0.6,
      created_at: daysAgo(1, 10, 30),
      words: null,
    },
  ];
}

function daysAgo(days: number, hour: number, minute = 0): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
}

function seedNote(
  domain: string,
  destination: string | null,
  body: string,
  createdAt: string,
  attachments: AttachmentOut[] = [],
  ingestState = "indexed",
  // Settled fixtures default to fully analyzed; pipeline-stage fixtures
  // override this to exercise the lifecycle chip's intermediate states.
  analyzed = ingestState === "indexed",
  provenance = "human",
): NoteOut {
  return {
    id: id("note"),
    client_id: id("client"),
    domain,
    destination,
    body,
    created_at: createdAt,
    tz_offset_minutes: null,
    ingest_state: ingestState,
    analyzed,
    provenance,
    hidden: false,
    attachments,
    latitude: null,
    longitude: null,
    accuracy_m: null,
  };
}

// Fully analyzed image: exercises the expansion's OCR inset + description.
const roofQuoteJpg = makeAttachment("roof-quote.jpg", "image/jpeg", true, true);
attachmentExtracts.set(roofQuoteJpg.id, extractFixtures());

// Oldest-first internally; the list endpoint serves newest-first.
const notes: NoteOut[] = [
  seedNote(
    "general",
    null,
    "Dad: grandpa worked at the mill in Ohio before the war — family history follow-up",
    daysAgo(8, 19, 12),
  ),
  seedNote(
    "finance",
    "Statements",
    "Q2 brokerage statement filed — rebalance overdue",
    daysAgo(8, 20, 40),
    [makeAttachment("brokerage-q2.pdf", "application/pdf")],
  ),
  seedNote(
    "health",
    "Medications",
    "Started vitamin D 2000 IU daily per Dr. Akin",
    daysAgo(3, 8, 5),
  ),
  seedNote("general", null, "Book rec from Sam: The Beginning of Infinity", daysAgo(3, 13, 30)),
  seedNote(
    "finance",
    "Receipts",
    "Roof repair quote — $4,200 incl. flashing. Get second opinion?",
    daysAgo(1, 10, 22),
    // OCR + description cached: exercises the "text + description" chip.
    [roofQuoteJpg],
  ),
  seedNote("general", null, "Garage door keypad battery replaced — CR2032", daysAgo(1, 17, 48)),
  seedNote(
    "general",
    null,
    "Groceries: eggs, coffee, olive oil, that bread Mom liked",
    daysAgo(0, 8, 15),
    [],
    "indexed",
    // Indexed but pre-analysis: exercises the "analyzing…" chip.
    false,
  ),
  seedNote(
    "general",
    null,
    "Whiteboard from the planning session — decisions are in the photo",
    daysAgo(0, 9, 10),
    // Image with an empty vision cache: exercises the "reading image…" chip.
    [makeAttachment("whiteboard.jpg", "image/jpeg")],
    "indexed",
    false,
  ),
  seedNote(
    "health",
    "Labs",
    "Annual physical — BP 118/76. Lab orders attached.\n\nDr. Akin wants a follow-up fasting panel in 3 months; book the draw early morning. Ask about the vitamin D dose then too.",
    daysAgo(0, 10, 5),
    [makeAttachment("lab-orders.pdf", "application/pdf")],
    "failed",
  ),
  seedNote(
    "general",
    null,
    "Long-form capture to exercise the 3-line clamp: the contractor said the south fence posts are rotted at the base, the gate hinge needs a longer lag bolt, and the section behind the shed should really be replaced whole rather than patched — he can quote both options next week, but materials prices change monthly so don't sit on it.",
    daysAgo(0, 11, 40),
  ),
  seedNote(
    "general",
    null,
    "Call the dentist about the crown — left side",
    daysAgo(0, 12, 10),
    [],
    "pending",
  ),
];

// The one fully-analyzed fixture note: drives the Analysis tab, the entity
// pages it links to, and several review-inbox items.
const PATEL_BODY =
  "Saw Dr. Patel this morning — BP 128/82, she wants a follow-up in three months (September). " +
  "Sarah drove me over; she's mostly moved into the new Denver place now. " +
  "Patel says keep up the morning walks.";

const patelNote = seedNote("health", "Records", PATEL_BODY, daysAgo(0, 9, 40));
notes.push(patelNote);

// ===== Phase 6 fixture: the wiki reader's Priya Nair article =====
// The example mock (docs/mocks/wiki-reader-example-priya.html) verbatim: lead,
// type-guided sections with nested subsections, a bulleted list, two tables, and
// the 20 numbered references the inline [n] superscripts cite.
const PRIYA_ARTICLE: WikiArticleOut = {
  id: "priya-nair",
  title: "Priya Nair",
  subtitle: "Person · pediatrician · machine-written from your notes",
  infobox: {
    title: "Priya Nair",
    photo: true,
    fields: [
      { label: "Born", value: "Austin", citations: [1], link: true },
      { label: "Sibling", value: "Jordan Hale (br.)", citations: [2], link: true },
      { label: "Spouse", value: "Tom (m. 2021)", citations: [3], redLink: true },
      { label: "Children", value: "Anaya, Mira", citations: [7], redLink: true },
      { label: "Occupation", value: "Pediatrician", citations: [4] },
      { label: "Practice", value: "Nair Pediatrics (2024–)", citations: [9], link: true },
    ],
  },
  lead: [
    {
      kind: "p",
      text: "Priya Nair is a pediatrician and the founder of [Nair Pediatrics](wiki:nair-pediatrics) in [Brookline](wiki:brookline).[9] She is the younger sister of [Jordan Hale](wiki:jordan-hale).[2]",
    },
  ],
  sections: [
    {
      heading: "Early life",
      domain: "general",
      blocks: [
        {
          kind: "p",
          text: "Nair grew up in [Austin](wiki:austin), Texas, where she was known within the family as a science enthusiast from an early age.[1] In 2019 she was admitted to medical school at [Johns Hopkins](wiki:johns-hopkins).[2]",
        },
      ],
    },
    {
      heading: "Career",
      domain: "general",
      blocks: [
        {
          kind: "p",
          text: "Nair is a pediatrician who trained at a children's clinic before founding her own practice.",
        },
      ],
      subsections: [
        {
          heading: "Training and early career",
          blocks: [
            {
              kind: "p",
              text: "After completing her residency in 2022, Nair worked as a pediatrician at [Riverside Children's Clinic](wiki:riverside).[4]",
            },
          ],
        },
        {
          heading: "Nair Pediatrics",
          blocks: [
            {
              kind: "p",
              text: "In 2024 she left Riverside to open her own practice, [Nair Pediatrics](wiki:nair-pediatrics), in [Brookline](wiki:brookline), where she currently practices.[9]",
            },
          ],
        },
        {
          heading: "Talks and publications",
          blocks: [
            {
              kind: "ul",
              items: [
                "Co-authored a paper on vaccine hesitancy in JAMA Pediatrics (2023).[16]",
                "Presented at the regional conference on childhood nutrition (2024).[17]",
                "Gave a talk at the state pediatric conference on childhood asthma (2025).[13]",
              ],
            },
          ],
        },
      ],
    },
    {
      heading: "Personal life",
      domain: "general",
      blocks: [
        {
          kind: "p",
          text: "Nair married [Tom](redlink) in a small courthouse ceremony in 2021, attended only by family.[3] Their first daughter, [Anaya](redlink), was born in 2024,[7] and their second, [Mira](redlink), in 2026.[20] In 2025 the family moved to a larger home in [Brookline](wiki:brookline).[12]",
        },
        {
          kind: "p",
          text: "Nair is an accomplished marathon runner, improving her time in each successive race:",
        },
        {
          kind: "table",
          header: ["Event", "Year", "Time"],
          rows: [
            ["Boston", "2023", "3:52[5]"],
            ["Chicago", "2024", "3:47[14]"],
            ["NYC", "2025", "3:41[15]"],
          ],
        },
        {
          kind: "p",
          text: "She is also regarded within the family as its best cook, known for her biryani.[10]",
        },
      ],
    },
    {
      heading: "Health",
      domain: "health",
      blocks: [
        {
          kind: "p",
          text: "Nair has a serious peanut allergy and carries an EpiPen.[6] She takes the following medications:",
        },
        {
          kind: "table",
          header: ["Medication", "Dose", "For"],
          rows: [
            ["Levothyroxine", "50 mcg", "Thyroid[18]"],
            ["Cetirizine", "10 mg daily (spring)", "Seasonal allergies[19]"],
          ],
        },
      ],
    },
    {
      heading: "Finances",
      domain: "finance",
      blocks: [
        {
          kind: "p",
          text: "In 2024 [Jordan Hale](wiki:jordan-hale) lent Nair $4,000 for clinic equipment[8]; the loan was repaid in full in 2025.[11]",
        },
      ],
    },
  ],
  references: [
    {
      n: 1,
      note_id: "note-priya-1",
      meta: "Note · May 2, 2018",
      domain: "general",
      snippet: "Priya <mark>grew up in Austin</mark> — always the science nerd.",
    },
    {
      n: 2,
      note_id: "note-priya-2",
      meta: "Note · Jun 10, 2019",
      domain: "general",
      snippet: "got into <mark>med school at Johns Hopkins</mark>! my little sister.",
    },
    {
      n: 3,
      note_id: "note-priya-3",
      meta: "Note · Sep 2, 2021",
      domain: "general",
      snippet: "Priya <mark>married Tom</mark> at the courthouse — just family.",
    },
    {
      n: 4,
      note_id: "note-priya-4",
      meta: "Note · Mar 15, 2022",
      domain: "general",
      snippet: "residency, started as a <mark>pediatrician at Riverside</mark>.",
    },
    {
      n: 5,
      note_id: "note-priya-5",
      meta: "Note · Apr 18, 2023",
      domain: "general",
      snippet: "ran the <mark>Boston Marathon — 3:52</mark>!",
    },
    {
      n: 6,
      note_id: "note-priya-6",
      meta: "Note · Nov 20, 2023",
      domain: "health",
      snippet: "carry her EpiPen — <mark>peanut allergy</mark> is serious.",
    },
    {
      n: 7,
      note_id: "note-priya-7",
      meta: "Note · Jan 30, 2024",
      domain: "general",
      snippet: "Priya and Tom had a <mark>baby girl, Anaya</mark>.",
    },
    {
      n: 8,
      note_id: "note-priya-8",
      meta: "Note · Jul 12, 2024",
      domain: "finance",
      snippet: "<mark>Lent Priya $4,000</mark> for clinic equipment.",
    },
    {
      n: 9,
      note_id: "note-priya-9",
      meta: "Note · Sep 5, 2024",
      domain: "general",
      snippet: "<mark>left Riverside to open Nair Pediatrics in Brookline</mark>.",
    },
    {
      n: 10,
      note_id: "note-priya-10",
      meta: "Note · Dec 1, 2024",
      domain: "general",
      snippet: "Priya's <mark>biryani</mark> — best cook in the family.",
    },
    {
      n: 11,
      note_id: "note-priya-11",
      meta: "Note · May 20, 2025",
      domain: "finance",
      snippet: "<mark>paid back the $4,000 loan in full</mark>.",
    },
    {
      n: 12,
      note_id: "note-priya-12",
      meta: "Note · Aug 10, 2025",
      domain: "general",
      snippet: "Priya and Tom <mark>moved to a bigger place in Brookline</mark>.",
    },
    {
      n: 13,
      note_id: "note-priya-13",
      meta: "Note · Oct 1, 2025",
      domain: "general",
      snippet: "<mark>talk at the state pediatric conference on childhood asthma</mark>.",
    },
    {
      n: 14,
      note_id: "note-priya-14",
      meta: "Note · Oct 13, 2024",
      domain: "general",
      snippet: "ran the <mark>Chicago Marathon — 3:47</mark>, a PR!",
    },
    {
      n: 15,
      note_id: "note-priya-15",
      meta: "Note · Nov 2, 2025",
      domain: "general",
      snippet: "finished the <mark>NYC Marathon in 3:41</mark>.",
    },
    {
      n: 16,
      note_id: "note-priya-16",
      meta: "Note · Jun 1, 2023",
      domain: "general",
      snippet: "co-authored a <mark>paper on vaccine hesitancy in JAMA Pediatrics</mark>.",
    },
    {
      n: 17,
      note_id: "note-priya-17",
      meta: "Note · May 20, 2024",
      domain: "general",
      snippet: "<mark>presented at the regional conference on childhood nutrition</mark>.",
    },
    {
      n: 18,
      note_id: "note-priya-18",
      meta: "Note · Mar 10, 2024",
      domain: "health",
      snippet: "started <mark>levothyroxine 50mcg</mark> for her thyroid.",
    },
    {
      n: 19,
      note_id: "note-priya-19",
      meta: "Note · Apr 22, 2025",
      domain: "health",
      snippet: "takes <mark>cetirizine 10mg</mark> each day in spring.",
    },
    {
      n: 20,
      note_id: "note-priya-20",
      meta: "Note · Feb 9, 2026",
      domain: "general",
      snippet: "Priya and Tom had a <mark>second girl, Mira</mark>.",
    },
  ],
};

// A compact, valid article for the landing's secondary entries, so every row is
// navigable in the mock (Priya is the rich worked example; these are stubs).
function stubArticle(
  id: string,
  title: string,
  kind: string,
  domain: string,
  blurb: string,
): WikiArticleOut {
  return {
    id,
    title,
    subtitle: `${kind} · machine-written from your notes`,
    infobox: { title, kind, fields: [] },
    lead: [{ kind: "p", text: `${blurb}[1]` }],
    sections: [],
    references: [
      {
        n: 1,
        note_id: `note-${id}-1`,
        meta: "Note · 2024",
        domain,
        snippet: `captured in a note about <mark>${title}</mark>.`,
      },
    ],
  };
}

const WIKI_ARTICLES: Record<string, WikiArticleOut> = {
  [PRIYA_ARTICLE.id]: PRIYA_ARTICLE,
  "celine-hopkins": stubArticle(
    "celine-hopkins",
    "Celine Hopkins",
    "Person",
    "general",
    "Software engineer at Globex; the owner's spouse.",
  ),
  "globex-corp": stubArticle(
    "globex-corp",
    "Globex Corporation",
    "Organization",
    "general",
    "Tech company; Celine's employer since 2019.",
  ),
  "nair-pediatrics": stubArticle(
    "nair-pediatrics",
    "Nair Pediatrics",
    "Organization",
    "general",
    "Priya's pediatric practice in Brookline, opened 2024.",
  ),
  brookline: stubArticle(
    "brookline",
    "Brookline",
    "Place",
    "general",
    "Massachusetts town; where Priya lives and practices.",
  ),
  denver: stubArticle("denver", "Denver", "Place", "general", "Colorado city; Celine's home."),
};

// The wiki landing (docs/mocks/wiki-landing-a-search-rails.html): derived rails
// over the article set — recently-updated, most-connected hubs, type index.
const WIKI_LANDING: WikiLandingOut = {
  recent: [
    {
      id: "priya-nair",
      title: "Priya Nair",
      kind: "Person",
      domain: "general",
      blurb: "Pediatrician; the owner's younger sister; founder of Nair Pediatrics.",
      when: "updated 2h ago",
    },
    {
      id: "globex-corp",
      title: "Globex Corporation",
      kind: "Organization",
      domain: "general",
      blurb: "Tech company; Celine's employer since 2019.",
      when: "yesterday",
    },
    {
      id: "brookline",
      title: "Brookline",
      kind: "Place",
      domain: "general",
      blurb: "Massachusetts town; where Priya lives and practices.",
      when: "3 days ago",
    },
  ],
  hubs: [
    {
      id: "celine-hopkins",
      title: "Celine Hopkins",
      kind: "Person",
      domain: "general",
      blurb: "Software engineer at Globex; the owner's spouse.",
      links: 12,
    },
    {
      id: "globex-corp",
      title: "Globex Corporation",
      kind: "Organization",
      domain: "general",
      blurb: "Tech company; Celine's employer since 2019.",
      links: 9,
    },
    {
      id: "brookline",
      title: "Brookline",
      kind: "Place",
      domain: "general",
      blurb: "Massachusetts town; where Priya lives and practices.",
      links: 7,
    },
  ],
  groups: [
    {
      type: "People",
      entries: [
        {
          id: "celine-hopkins",
          title: "Celine Hopkins",
          kind: "Person",
          domain: "general",
          blurb: "Software engineer at Globex; the owner's spouse.",
        },
        {
          id: "priya-nair",
          title: "Priya Nair",
          kind: "Person",
          domain: "general",
          blurb: "Pediatrician; the owner's younger sister; founder of Nair Pediatrics.",
        },
      ],
    },
    {
      type: "Organizations",
      entries: [
        {
          id: "globex-corp",
          title: "Globex Corporation",
          kind: "Organization",
          domain: "general",
          blurb: "Tech company; Celine's employer since 2019.",
        },
        {
          id: "nair-pediatrics",
          title: "Nair Pediatrics",
          kind: "Organization",
          domain: "general",
          blurb: "Priya's pediatric practice in Brookline, opened 2024.",
        },
      ],
    },
    {
      type: "Places",
      entries: [
        {
          id: "brookline",
          title: "Brookline",
          kind: "Place",
          domain: "general",
          blurb: "Massachusetts town; where Priya lives and practices.",
        },
        {
          id: "denver",
          title: "Denver",
          kind: "Place",
          domain: "general",
          blurb: "Colorado city; Celine's home.",
        },
      ],
    },
  ],
};

// Flat list of every article, for the search wiki leg (the type index covers all).
const WIKI_INDEX = WIKI_LANDING.groups.flatMap((g) => g.entries);

// The Talk board (docs/mocks/wiki-talk-b-topics.html): per-article threaded topics + an auto
// Build-log. Mutated by the mock's new-topic / reply / resolve routes so dev exercises the loop.
const WIKI_TALK: Record<string, WikiTalkOut> = {
  "priya-nair": {
    title: "Priya Nair",
    topics: [
      {
        id: "topic-globex",
        kind: "discussion",
        title: "Outdated: still says she works at Globex",
        status: "open",
        meta: null,
        posts: [
          {
            id: "p-g1",
            author: "owner",
            body: "She left Globex in March. The Career section is wrong.",
            source: null,
            outcome: null,
            created_at: "2026-06-17T09:14:00Z",
            rev: null,
          },
          {
            id: "p-g2",
            author: "editor",
            body: '"Works at Globex" is sourced from one note with no later departure.',
            source: {
              note_id: "note-priya-9",
              meta: "Note · Jan 19, 2026",
              snippet: "promoted to senior engineer at Globex.",
              domain: "general",
            },
            outcome: null,
            created_at: "2026-06-17T09:14:30Z",
            rev: null,
          },
          {
            id: "p-g3",
            author: "owner",
            body: "File a correction — left Globex March 2026.",
            source: null,
            outcome: "correction note filed → rebuild queued",
            created_at: "2026-06-17T09:16:00Z",
            rev: null,
          },
        ],
      },
      {
        id: "topic-addr",
        kind: "discussion",
        title: "Drop the old apartment address?",
        status: "resolved",
        meta: null,
        posts: [
          {
            id: "p-a1",
            author: "owner",
            body: "Don't feature the old Boulder address.",
            source: null,
            outcome: null,
            created_at: "2026-03-12T10:00:00Z",
            rev: null,
          },
          {
            id: "p-a2",
            author: "editor",
            body: "Excluded that note from this article.",
            source: null,
            outcome: "source excluded · rebuilt",
            created_at: "2026-03-12T10:01:00Z",
            rev: null,
          },
        ],
      },
      {
        id: "topic-log",
        kind: "build_log",
        title: "Build log",
        status: "open",
        meta: "auto · 3 entries",
        posts: [
          {
            id: "p-l1",
            author: "builder",
            body: "Created article (Person guide); 11 facts across 3 domains.",
            source: null,
            outcome: null,
            created_at: "2026-03-02T02:11:00Z",
            rev: 1,
          },
          {
            id: "p-l2",
            author: "builder",
            body: "Excluded note (Boulder address) per discussion; rewrote Personal life.",
            source: null,
            outcome: null,
            created_at: "2026-03-12T02:09:00Z",
            rev: 2,
          },
          {
            id: "p-l3",
            author: "builder",
            body: "Rebuilt article (Person guide); 12 facts across 3 domains.",
            source: null,
            outcome: null,
            created_at: "2026-03-17T02:14:00Z",
            rev: 3,
          },
        ],
      },
    ],
  },
};

// ===== Phase 3 fixtures: analysis, entities, review, usage =====

// The backend serves "provider:model" here, nothing fancier.
const EXTRACTOR = "xai:grok-4.3";

function fact(over: Partial<FactOut> & Pick<FactOut, "id" | "predicate">): FactOut {
  return {
    entity_id: "ent-me",
    entity_name: "Me",
    qualifier: null,
    kind: "state",
    statement: "",
    value_json: null,
    assertion: "asserted",
    status: "active",
    pinned: false,
    confidence: 0.9,
    valid_from: patelNote.created_at,
    valid_to: null,
    reported_at: patelNote.created_at,
    temporal_precision: "day",
    object_entity_id: null,
    object_entity_name: null,
    source_snippet: null,
    ...over,
  };
}

const FACT_BP = fact({
  id: "fact-bp-0610",
  predicate: "blood_pressure",
  kind: "measurement",
  statement: "Blood pressure measured 128/82 mmHg at Dr. Patel's office on June 10, 2026.",
  value_json: { systolic: 128, diastolic: 82, unit: "mmHg" },
  confidence: 0.97,
  temporal_precision: "instant",
  source_snippet: "Saw Dr. Patel this morning — <mark>BP 128/82</mark>, she wants a follow-up",
});

const FACT_VISIT = fact({
  id: "fact-visit-0610",
  predicate: "medical_visit",
  kind: "event",
  statement: "Office visit with Dr. Patel on June 10, 2026.",
  value_json: "office visit — Dr. Patel",
  confidence: 0.95,
  source_snippet: "<mark>Saw Dr. Patel this morning</mark> — BP 128/82",
});

const FACT_FOLLOWUP = fact({
  id: "fact-followup-time",
  entity_id: "ent-followup",
  entity_name: "Dr. Patel follow-up",
  predicate: "scheduled_time",
  kind: "state",
  statement: "The Dr. Patel follow-up is expected in September 2026.",
  value_json: "Sep 2026",
  assertion: "expected",
  confidence: 0.93,
  valid_from: "2026-09-01T00:00:00Z",
  temporal_precision: "month",
  source_snippet: "she wants a <mark>follow-up in three months (September)</mark>",
});

const FACT_SARAH_DENVER = fact({
  id: "fact-sarah-addr-denver",
  entity_id: "ent-sarah",
  entity_name: "Sarah",
  predicate: "address",
  qualifier: "home",
  kind: "state",
  statement: "Sarah's home address is in Denver, CO as of June 2026.",
  value_json: "Denver, CO",
  status: "pending_review",
  confidence: 0.88,
  valid_from: "2026-06-01T00:00:00Z",
  temporal_precision: "month",
  source_snippet:
    "Sarah drove me over; she's mostly <mark>moved into the new Denver place</mark> now",
});

const FACT_PHYSICIAN = fact({
  id: "fact-me-physician",
  predicate: "physician",
  kind: "relationship",
  statement: "Dr. Patel is Jeff's physician.",
  value_json: "Dr. Patel",
  pinned: true,
  confidence: 0.99,
  valid_from: "2025-11-02T00:00:00Z",
  temporal_precision: "month",
  source_snippet: "<mark>Saw Dr. Patel</mark> this morning — BP 128/82",
});

const FACT_WALKS = fact({
  id: "fact-me-walks",
  predicate: "preferred_exercise",
  kind: "preference",
  statement: "Jeff keeps up morning walks, per Dr. Patel.",
  value_json: "morning walks",
  assertion: "reported",
  confidence: 0.82,
  source_snippet: "Patel says keep up the <mark>morning walks</mark>",
});

const ANALYSES: Record<string, NoteAnalysis> = {
  [patelNote.id]: {
    note_id: patelNote.id,
    title: "Dr. Patel visit — BP 128/82, follow-up in September",
    tags: ["blood-pressure", "dr-patel", "follow-up", "sarah"],
    analyzed_at: daysAgo(0, 9, 43),
    extractor: EXTRACTOR,
    facts: [FACT_BP, FACT_VISIT, FACT_FOLLOWUP, FACT_SARAH_DENVER, FACT_PHYSICIAN, FACT_WALKS],
    entities: [
      { id: "ent-me", kind: "Person", name: "Me", status: "active" },
      { id: "ent-patel", kind: "Person", name: "Dr. Patel", status: "active" },
      { id: "ent-sarah", kind: "Person", name: "Sarah", status: "active" },
      {
        id: "ent-followup",
        kind: "appointment",
        name: "Dr. Patel follow-up",
        status: "provisional",
      },
    ],
    temporal_tokens: [
      {
        id: "tok-followup",
        surface_phrase: "in three months (September)",
        kind: "point",
        resolved_start: "2026-09-01T00:00:00Z",
        resolved_end: null,
        temporal_precision: "month",
      },
      {
        id: "tok-this-morning",
        surface_phrase: "this morning",
        kind: "point",
        resolved_start: patelNote.created_at,
        resolved_end: null,
        temporal_precision: "day",
      },
    ],
  },
};

const FACT_SARAH_AUSTIN = fact({
  id: "fact-sarah-addr-austin",
  entity_id: "ent-sarah",
  entity_name: "Sarah",
  predicate: "address",
  qualifier: "home",
  kind: "state",
  statement: "Sarah's home address was in Austin, TX from March 2023 to June 2026.",
  value_json: "Austin, TX",
  status: "superseded",
  confidence: 0.94,
  valid_from: "2023-03-01T00:00:00Z",
  valid_to: "2026-06-01T00:00:00Z",
  reported_at: "2023-03-12T18:20:00Z",
  temporal_precision: "month",
  source_snippet: "helped Sarah move the last boxes into the <mark>Austin apartment</mark>",
});

const FACT_SARAH_EMPLOYER = fact({
  id: "fact-sarah-employer",
  entity_id: "ent-sarah",
  entity_name: "Sarah",
  predicate: "worksFor",
  kind: "relationship",
  statement: "Sarah works for Ridgeline Architects.",
  value_json: "Ridgeline Architects",
  confidence: 0.91,
  valid_from: "2024-01-08T00:00:00Z",
  reported_at: "2024-01-08T19:00:00Z",
  temporal_precision: "month",
  source_snippet: "Sarah started at <mark>Ridgeline Architects</mark> this week",
});

const FACT_SARAH_BDAY = fact({
  id: "fact-sarah-bday",
  entity_id: "ent-sarah",
  entity_name: "Sarah",
  predicate: "birthDate",
  kind: "attribute",
  statement: "Sarah's birthday is March 14, 1988.",
  value_json: "March 14, 1988",
  confidence: 0.96,
  valid_from: null,
  reported_at: "2025-03-14T16:00:00Z",
  temporal_precision: "day",
  source_snippet: "card in the mail for <mark>Sarah's birthday on the 14th</mark>",
});

const FACT_BP_OLD = fact({
  id: "fact-bp-0607",
  predicate: "blood_pressure",
  kind: "measurement",
  statement: "Blood pressure measured 118/76 mmHg at the annual physical.",
  value_json: { systolic: 118, diastolic: 76, unit: "mmHg" },
  confidence: 0.95,
  valid_from: daysAgo(3, 10, 5),
  reported_at: daysAgo(3, 10, 5),
  temporal_precision: "instant",
  source_snippet: "Annual physical — <mark>BP 118/76</mark>. Lab orders attached.",
});

const FACT_PATEL_SPECIALTY = fact({
  id: "fact-patel-specialty",
  entity_id: "ent-patel",
  entity_name: "Dr. Patel",
  predicate: "medicalSpecialty",
  kind: "attribute",
  statement: "Dr. Patel practices internal medicine.",
  value_json: "internal medicine",
  confidence: 0.89,
  valid_from: null,
  reported_at: "2025-11-02T15:00:00Z",
  source_snippet: "new <mark>internal medicine</mark> doc — Dr. Patel",
});

const ENTITIES: Record<string, EntityOut> = {
  "ent-me": {
    id: "ent-me",
    kind: "Person",
    canonical_name: "Me",
    status: "active",
    aliases: ["Jeff"],
    domain: "general",
    predicates: [
      {
        predicate: "blood_pressure",
        qualifier: null,
        current: FACT_BP,
        history: [FACT_BP, FACT_BP_OLD],
      },
      {
        predicate: "physician",
        qualifier: null,
        current: FACT_PHYSICIAN,
        history: [FACT_PHYSICIAN],
      },
      {
        predicate: "preferred_exercise",
        qualifier: null,
        current: FACT_WALKS,
        history: [FACT_WALKS],
      },
    ],
    inbound: [
      {
        entity_id: "ent-sarah",
        name: "Sarah",
        predicate: "sibling",
        statement: "Sarah is Jeff's sister.",
      },
    ],
    mentions: [
      {
        note_id: patelNote.id,
        snippet: "<mark>Saw Dr. Patel this morning</mark> — BP 128/82",
        created_at: patelNote.created_at,
      },
    ],
  },
  "ent-patel": {
    id: "ent-patel",
    kind: "Person",
    canonical_name: "Dr. Patel",
    status: "active",
    aliases: ["Patel"],
    domain: "health",
    predicates: [
      {
        predicate: "medicalSpecialty",
        qualifier: null,
        current: FACT_PATEL_SPECIALTY,
        history: [FACT_PATEL_SPECIALTY],
      },
    ],
    inbound: [
      {
        entity_id: "ent-me",
        name: "Me",
        predicate: "physician",
        statement: "Dr. Patel is Jeff's physician.",
      },
    ],
    mentions: [
      {
        note_id: patelNote.id,
        snippet: "Saw <mark>Dr. Patel</mark> this morning — BP 128/82",
        created_at: patelNote.created_at,
      },
    ],
  },
  "ent-sarah": {
    id: "ent-sarah",
    kind: "Person",
    canonical_name: "Sarah Hopkins",
    status: "active",
    aliases: ["Sarah", "sis"],
    domain: "general",
    predicates: [
      {
        predicate: "address",
        qualifier: "home",
        current: FACT_SARAH_DENVER,
        history: [FACT_SARAH_DENVER, FACT_SARAH_AUSTIN],
      },
      {
        predicate: "worksFor",
        qualifier: null,
        current: FACT_SARAH_EMPLOYER,
        history: [FACT_SARAH_EMPLOYER],
      },
      {
        predicate: "birthDate",
        qualifier: null,
        current: FACT_SARAH_BDAY,
        history: [FACT_SARAH_BDAY],
      },
    ],
    inbound: [
      {
        entity_id: "ent-me",
        name: "Me",
        predicate: "sibling",
        statement: "Sarah is Jeff's sister.",
      },
    ],
    mentions: [
      {
        note_id: patelNote.id,
        snippet:
          "<mark>Sarah</mark> drove me over; she's mostly moved into the new Denver place now.",
        created_at: patelNote.created_at,
      },
      {
        note_id: "note-archived-77",
        snippet: "Helped <mark>Sarah</mark> move the last boxes out of the Austin apartment.",
        created_at: "2026-03-02T17:40:00Z",
      },
    ],
  },
  "ent-followup": {
    id: "ent-followup",
    kind: "appointment",
    canonical_name: "Dr. Patel follow-up",
    status: "provisional",
    aliases: [],
    domain: "health",
    predicates: [
      {
        predicate: "scheduled_time",
        qualifier: null,
        current: FACT_FOLLOWUP,
        history: [FACT_FOLLOWUP],
      },
    ],
    inbound: [],
    mentions: [
      {
        note_id: patelNote.id,
        snippet: "she wants a <mark>follow-up in three months (September)</mark>",
        created_at: patelNote.created_at,
      },
    ],
  },
};

// Review queue: one of every kind, payloads mirroring what the backend
// writes at item creation: the row ids its resolution handlers read plus
// the display fields the card renders (summary / snippet / outcomes /
// choices / *_destructive flags). Invariant shared with the backend: every
// advertised choice action and outcome verb is exactly an action
// POST /review/{id}/resolve accepts — collisions resolve through
// accept_a/accept_b choices and advertise no footer verbs.
function openReview(item: Omit<ReviewItem, "status" | "resolution" | "resolved_at">): ReviewItem {
  return { ...item, status: "open", resolution: null, resolved_at: null };
}

const REVIEW_ITEMS: ReviewItem[] = [
  openReview({
    id: "rev-1",
    kind: "attribute_collision",
    domain: "general",
    created_at: daysAgo(0, 9, 45),
    payload: {
      fact_a: "fact-sarah-bday-1990",
      fact_b: FACT_SARAH_BDAY.id,
      predicate: "birthDate",
      qualifier: "",
      fact_kind: "attribute",
      assertion: "asserted",
      statement: "Sarah's birthday is March 14, 1988.",
      value_json: { value: "March 14, 1988" },
      entity_ref: "sarah",
      note_id: patelNote.id,
      summary: "two values recorded for Sarah's birthDate",
      rationale: "a card in this note dates Sarah's birthday differently than the wiki.",
      confidence: 0.74,
      snippet: "card in the mail for <mark>Sarah's birthday on the 14th</mark>",
      choices: [
        { action: "accept_a", label: "May 2, 1990", detail: "previously recorded" },
        { action: "accept_b", label: "March 14, 1988", detail: "from this note" },
      ],
    },
  }),
  openReview({
    id: "rev-2",
    kind: "merge_proposal",
    domain: "general",
    created_at: daysAgo(1, 12, 0),
    payload: {
      entity_a: "ent-robert-chen",
      entity_b: "ent-bob",
      summary: "are “Bob” and “Robert Chen” the same person?",
      snippet:
        "Lunch with <mark>Bob</mark> — he's pitching the Donnelly account again next quarter.",
      outcomes: {
        accept:
          "bob and robert chen become one person — mentions repoint; a later split can undo it.",
        reject: "writes a permanent distinct-from edge — this pair is never proposed again.",
      },
      reject_destructive: true,
    },
  }),
  openReview({
    id: "rev-3",
    kind: "ambiguous_mention",
    domain: "general",
    created_at: daysAgo(1, 10, 30),
    payload: {
      name: "Sam",
      note_id: "note-archived-91",
      entity_ids: ["ent-sam-rivera", "ent-sam-okafor"],
      summary: "which Sam?",
      snippet: "<mark>Sam</mark> said the roof quote covers the flashing too.",
      // accept is not advertised: linking a pick needs layer-2/3 resolution.
      outcomes: {
        reject: "the mention stays unlinked — it can be re-proposed with more signal.",
      },
    },
  }),
  openReview({
    id: "rev-4",
    kind: "domain_promotion",
    domain: "health",
    created_at: daysAgo(2, 8, 15),
    payload: {
      fact_id: "fact-akin-fax",
      note_id: "note-archived-88",
      note_domain: "health",
      proposed_domain: "general",
      summary: "this faxRequest fact may belong in general, not health",
      snippet: "Asked <mark>Dr. Akin</mark>'s office to fax the form to the school nurse.",
      outcomes: {
        accept: "the fact moves to general and is pinned there — reprocessing can't pull it back.",
        reject: "the fact stays in health — the note's firewall keeps it.",
      },
    },
  }),
  openReview({
    id: "rev-5",
    kind: "fact_conflict",
    domain: "health",
    created_at: daysAgo(0, 11, 5),
    payload: {
      fact_a: FACT_BP.id,
      fact_b: "fact-bp-kiosk",
      predicate: "blood_pressure",
      qualifier: "",
      fact_kind: "measurement",
      assertion: "asserted",
      statement: "Pharmacy kiosk blood pressure was 138/92 mmHg.",
      value_json: { systolic: 138, diastolic: 92, unit: "mmHg" },
      entity_ref: "me",
      note_id: patelNote.id,
      summary: "two blood_pressure values disagree for Me",
      snippet:
        "Pharmacy kiosk says <mark>138/92</mark> — way off this morning's 128/82 at Dr. Patel's.",
      choices: [
        { action: "accept_a", label: "128/82 mmHg", detail: "previously recorded" },
        { action: "accept_b", label: "138/92 mmHg", detail: "from this note" },
      ],
    },
  }),
  // rev-6/rev-7: kinds the schema reserves but no pipeline writes yet; they
  // keep the card's rarer states (low-confidence copy, destructive accept)
  // exercised in mock mode and follow the same payload convention.
  openReview({
    id: "rev-6",
    kind: "low_confidence",
    domain: "health",
    created_at: daysAgo(3, 8, 10),
    payload: {
      summary: "low-confidence extraction (41%)",
      rationale: "extracted from an uncertain phrasing — verify against the note.",
      confidence: 0.41,
      snippet: "Started <mark>vitamin D 2000 IU daily</mark> per Dr. Akin.",
      outcomes: {
        accept: "the fact stands and gets pinned — reprocessing can't drop it.",
        reject: "the fact is retracted as a misread.",
      },
    },
  }),
  openReview({
    id: "rev-inf",
    kind: "low_confidence_inference",
    domain: "general",
    created_at: daysAgo(0, 20, 33),
    payload: {
      note_id: patelNote.id,
      entity_ref: "me",
      predicate: "name.nickname",
      qualifier: "",
      fact_kind: "attribute",
      statement: "People call me Jeff.",
      value_json: { name: "Jeff" },
      weight: 0.6,
      reasons: ["below_threshold"],
      fact_id: "fact-nickname-jeff",
      summary: "hold for review (below_threshold): People call me Jeff.",
      snippet: "<mark>People call me Jeff</mark>.",
      outcomes: {
        accept: "the fact is recorded and pinned — reprocessing won't drop it.",
        reject: "the fact is discarded.",
      },
    },
  }),
  openReview({
    id: "rev-7",
    kind: "split_proposal",
    domain: "finance",
    created_at: daysAgo(4, 18, 0),
    payload: {
      summary: "“the Honda” may be two different cars",
      snippet: "Oil change on <mark>the Honda</mark> — 152k miles now, the other receipt said 48k.",
      entity_name: "the Honda",
      outcomes: {
        accept: "the entity splits into two vehicles — mentions re-resolve from their spans.",
        reject: "stays one car; the mileage conflict goes back to fact review.",
      },
      accept_destructive: true,
    },
  }),
  openReview({
    id: "rev-8",
    kind: "extraction_truncated",
    domain: "general",
    created_at: daysAgo(0, 9, 40),
    payload: {
      note_id: patelNote.id,
      summary: "this note hit its fact budget — kept 40, skipped 6 facts",
      snippet:
        "Dad's full medical history — <mark>1998 appendectomy, 2011 ACL repair</mark>, and more…",
      // Informational, like a low-confidence notice: it wrote no graph state,
      // so its only verb (reject) is a dismissal. Re-run captures the tail.
      outcomes: {
        reject: "the note is left as-is — re-run analysis to capture more of it.",
      },
    },
  }),
  // Past decisions seed the resolved segment (newest first when listed):
  // an accepted merge, a decided collision, a rejected merge (permanent
  // distinct_from — its reopen keeps the edge), and a muted dismissal.
  {
    id: "rev-done-1",
    kind: "merge_proposal",
    domain: "general",
    created_at: daysAgo(1, 8, 30),
    status: "resolved",
    resolved_at: daysAgo(0, 9, 18),
    resolution: {
      action: "accept",
      payload: {},
      effects: [
        {
          action: "merged",
          entity_id: "ent-patel-dup",
          into: "ent-patel",
          prior_status: "provisional",
          prior_merged_into: null,
          mention_ids: ["men-patel-dup-1"],
          fact_ids: [],
          object_fact_ids: [],
        },
      ],
    },
    payload: {
      entity_a: "ent-patel",
      entity_b: "ent-patel-dup",
      summary: "merge “Dr. Patel” with “Dr. Anita Patel”",
      snippet:
        "follow-up booked with <mark>Dr. Patel</mark> for the 24th — same office as the Anita Patel visit in March.",
      outcomes: {
        accept: "they become one person — Dr. Anita Patel is canonical, mentions repoint.",
        reject: "writes a permanent distinct-from edge — this pair is never proposed again.",
      },
      reject_destructive: true,
    },
  },
  {
    id: "rev-done-2",
    kind: "attribute_collision",
    domain: "health",
    created_at: daysAgo(1, 17, 50),
    status: "resolved",
    resolved_at: daysAgo(1, 18, 2),
    resolution: {
      action: "accept_a",
      payload: { choice: "128 mg/dL" },
      effects: [
        {
          action: "pinned",
          fact_id: "fact-ldl-128",
          prior_status: "pending_review",
          prior_pinned: false,
          prior_superseded_by: null,
        },
        { action: "retracted", fact_id: "fact-ldl-132", prior_status: "pending_review" },
      ],
    },
    payload: {
      fact_a: "fact-ldl-128",
      fact_b: "fact-ldl-132",
      predicate: "ldlCholesterol",
      summary: "two LDL values from the same lab visit",
      snippet: "Quest results in — <mark>LDL 128</mark>, though the portal PDF also lists 132.",
      choices: [
        { action: "accept_a", label: "128 mg/dL", detail: "summary page value" },
        { action: "accept_b", label: "132 mg/dL", detail: "detail page value" },
      ],
    },
  },
  {
    id: "rev-done-3",
    kind: "merge_proposal",
    domain: "finance",
    created_at: daysAgo(5, 9, 0),
    status: "resolved",
    resolved_at: daysAgo(5, 9, 40),
    resolution: {
      action: "reject",
      payload: {},
      effects: [
        { action: "distinct_from", a: "ent-chase-sapphire", b: "ent-chase-visa", inserted: true },
      ],
    },
    payload: {
      entity_a: "ent-chase-visa",
      entity_b: "ent-chase-sapphire",
      summary: "merge “Chase Visa” with “Chase Sapphire”?",
      snippet: "Paid the <mark>Chase Visa</mark> — the Sapphire statement closes Friday.",
      outcomes: {
        accept: "the two cards become one account.",
        reject: "writes a permanent distinct-from edge — this pair is never proposed again.",
      },
      reject_destructive: true,
    },
  },
  {
    id: "rev-done-4",
    kind: "low_confidence",
    domain: "finance",
    created_at: daysAgo(2, 12, 0),
    status: "dismissed",
    resolved_at: daysAgo(2, 12, 30),
    resolution: { action: "dismiss", payload: {}, effects: [] },
    payload: {
      summary: "low-confidence extraction: “Roth contribution maxed”",
      snippet: "Think the <mark>Roth is maxed</mark> for the year? Need to check Fidelity first.",
      outcomes: {
        accept: "the fact stands at its stated confidence.",
        reject: "the extraction is dropped.",
      },
    },
  },
];

// Fake the backend's effects recording so dev:mock reopen round-trips.
function mockEffects(item: ReviewItem, action: string): Record<string, unknown>[] {
  const p = item.payload;
  if (
    (item.kind === "attribute_collision" || item.kind === "fact_conflict") &&
    (action === "accept_a" || action === "accept_b")
  ) {
    const winner = action === "accept_a" ? p.fact_a : p.fact_b;
    const loser = action === "accept_a" ? p.fact_b : p.fact_a;
    return [
      {
        action: "pinned",
        fact_id: winner,
        prior_status: "pending_review",
        prior_pinned: false,
        prior_superseded_by: null,
      },
      { action: "retracted", fact_id: loser, prior_status: "pending_review" },
    ];
  }
  if (item.kind === "merge_proposal" && action === "accept") {
    return [
      {
        action: "merged",
        entity_id: p.entity_b,
        into: p.entity_a,
        prior_status: "provisional",
        prior_merged_into: null,
        mention_ids: [],
        fact_ids: [],
        object_fact_ids: [],
      },
    ];
  }
  if (item.kind === "merge_proposal" && action === "reject") {
    const pair = [p.entity_a, p.entity_b].map(String).sort();
    return [{ action: "distinct_from", a: pair[0], b: pair[1], inserted: true }];
  }
  if (item.kind === "domain_promotion" && action === "accept") {
    return [
      {
        action: "domain_changed",
        fact_id: p.fact_id,
        prior_domain: p.note_domain,
        prior_pinned: false,
        new_domain: p.proposed_domain,
      },
    ];
  }
  return [];
}

/** Resolved-log ordering key: a reopened tombstone sorts by its marker. */
function decidedAt(item: ReviewItem): string {
  return item.resolved_at ?? item.resolution?.reopened_at ?? item.created_at;
}

/** The actions a row accepts: the universal dismiss/defer/discuss plus the
 * choices/outcomes its payload advertises — the backend's contract. */
function advertisedActions(item: ReviewItem): Set<string> {
  const advertised = new Set(["dismiss", "defer", "discuss", "correct"]);
  const choices = item.payload.choices;
  if (Array.isArray(choices)) {
    for (const choice of choices) {
      const action = (choice as { action?: unknown }).action;
      if (typeof action === "string") advertised.add(action);
    }
  }
  const outcomes = item.payload.outcomes;
  if (outcomes !== null && typeof outcomes === "object") {
    for (const verb of ["accept", "reject"]) {
      if (verb in outcomes) advertised.add(verb);
    }
  }
  return advertised;
}

/** Apply a resolution to fixture state with recorded effects, so triage,
 * defer, and reopen all round-trip in dev:mock. Returns the mutated item. */
function applyResolution(
  item: ReviewItem,
  action: string,
  payload: Record<string, unknown>,
): ReviewItem {
  const parked = action === "defer" || action === "discuss";
  const dismissal =
    action === "dismiss" || (item.kind === "ambiguous_mention" && action === "reject");
  const corrected = action === "correct";
  item.status = parked ? "deferred" : dismissal ? "dismissed" : "resolved";
  item.resolution = {
    action,
    payload,
    effects: corrected
      ? [{ action: "corrected", note_id: payload.note_id }]
      : parked || dismissal
        ? []
        : mockEffects(item, action),
  };
  item.resolved_at = new Date().toISOString();
  return item;
}

const LLM_USAGE: LlmUsage = {
  today: { input_tokens: 41_200, output_tokens: 12_400, cost_usd: 0.08 },
  month: { input_tokens: 1_240_000, output_tokens: 338_000, cost_usd: 2.41 },
  by_task: [
    { task: "note.extract", input_tokens: 982_000, output_tokens: 241_000, cost_usd: 1.83 },
    { task: "entity.disambiguate", input_tokens: 141_000, output_tokens: 52_000, cost_usd: 0.31 },
    { task: "fact.adjudicate", input_tokens: 88_400, output_tokens: 31_200, cost_usd: 0.21 },
    // A local model with no price-table entry: tokens only, never a guess.
    { task: "vision.ocr", input_tokens: 2_400_000, output_tokens: 14_000, cost_usd: null },
  ],
  days: Array.from({ length: 7 }, (_, i) => ({
    date: daysAgo(6 - i, 0).slice(0, 10),
    input_tokens: 120_000 + i * 31_000,
    output_tokens: 34_000 + i * 9_000,
    cost_usd: i === 2 ? null : 0.21 + i * 0.04,
  })),
};

// Analysis lands like the worker's would: an existing fixture keeps its
// facts with a bumped analyzed_at; other notes synthesize a minimal record.
function upsertAnalysis(noteId: string): void {
  ANALYSES[noteId] = {
    ...(ANALYSES[noteId] ?? emptyAnalysis(noteId)),
    analyzed_at: new Date().toISOString(),
    extractor: EXTRACTOR,
  };
}

function emptyAnalysis(noteId: string): NoteAnalysis {
  return {
    note_id: noteId,
    title: null,
    tags: [],
    analyzed_at: null,
    extractor: null,
    facts: [],
    entities: [],
    temporal_tokens: [],
  };
}

// Mirrors GET /api/entities: derives the browse rows from the entity-page
// fixtures, so the list and the pages it opens always agree in dev:mock.
function mockEntityList(params: URLSearchParams): EntityListItem[] {
  const q = (params.get("q") ?? "").toLowerCase();
  const kind = params.get("kind");
  return Object.values(ENTITIES)
    .filter((e) => e.status !== "merged")
    .filter((e) => kind === null || e.kind === kind)
    .filter(
      (e) =>
        q === "" ||
        e.canonical_name.toLowerCase().includes(q) ||
        e.aliases.some((a) => a.toLowerCase().includes(q)),
    )
    .map((e): EntityListItem => {
      const facts = e.predicates.flatMap((p) => p.history);
      return {
        id: e.id,
        kind: e.kind,
        canonical_name: e.canonical_name,
        status: e.status,
        fact_count: facts.filter((f) => f.status === "active" || f.status === "pending_review")
          .length,
        mention_count: e.mentions.length,
        last_seen: facts.map((f) => f.reported_at).sort()[facts.length - 1] ?? null,
      };
    })
    .sort((a, b) => {
      if (a.last_seen !== b.last_seen) {
        if (a.last_seen === null) return 1; // nulls last, like the SQL
        if (b.last_seen === null) return -1;
        return b.last_seen.localeCompare(a.last_seen);
      }
      return a.canonical_name.localeCompare(b.canonical_name);
    })
    .slice(0, 200);
}

// Mirrors GET /api/entities/{id}/neighbors: a BFS over the entity-page
// fixtures' relationship edges (outbound predicate facts + inbound edges) so
// the graph view and the pages it opens agree in dev:mock.
function mockNeighbors(rootId: string, depth: number): EgoGraph | null {
  if (!ENTITIES[rootId]) return null;
  const hops = Math.max(1, Math.min(depth, 2));
  const nodeIds = new Set<string>([rootId]);
  let frontier = new Set<string>([rootId]);
  const edges = new Map<string, GraphEdge>();
  for (let h = 0; h < hops && frontier.size > 0; h++) {
    const next = new Set<string>();
    const add = (source: string, target: string, predicate: string) => {
      edges.set(`${source}|${target}|${predicate}`, { source, target, predicate });
      for (const nid of [source, target]) {
        if (!nodeIds.has(nid)) {
          nodeIds.add(nid);
          next.add(nid);
        }
      }
    };
    for (const id of frontier) {
      const e = ENTITIES[id];
      if (!e) continue;
      for (const p of e.predicates) {
        const obj = p.current?.object_entity_id;
        if (obj && ENTITIES[obj]) add(id, obj, p.current?.predicate ?? p.predicate);
      }
      for (const ib of e.inbound) {
        if (ENTITIES[ib.entity_id]) add(ib.entity_id, id, ib.predicate);
      }
    }
    frontier = next;
  }
  const nodes = [...nodeIds].flatMap((id) => {
    const e = ENTITIES[id];
    return e
      ? [
          {
            id: e.id,
            kind: e.kind,
            canonical_name: e.canonical_name,
            status: e.status,
            domain: e.domain,
          },
        ]
      : [];
  });
  return { root: rootId, depth: hops, nodes, edges: [...edges.values()] };
}

// Mirrors GET /api/graph: the whole graph — every entity (including ones with
// no edges) plus all relationship edges, centered on "Me".
function mockFullGraph(): EgoGraph {
  const ids = Object.keys(ENTITIES);
  const edges = new Map<string, GraphEdge>();
  for (const e of Object.values(ENTITIES)) {
    for (const p of e.predicates) {
      const obj = p.current?.object_entity_id;
      if (obj && ENTITIES[obj]) {
        const predicate = p.current?.predicate ?? p.predicate;
        edges.set(`${e.id}|${obj}|${predicate}`, { source: e.id, target: obj, predicate });
      }
    }
  }
  const nodes = ids.map((id) => {
    const e = ENTITIES[id] as EntityOut;
    return {
      id: e.id,
      kind: e.kind,
      canonical_name: e.canonical_name,
      status: e.status,
      domain: e.domain,
    };
  });
  return { root: ENTITIES["ent-me"] ? "ent-me" : "", depth: 0, nodes, edges: [...edges.values()] };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const LATENCY_MS = 120;
const sleep = () => new Promise((resolve) => setTimeout(resolve, LATENCY_MS));

// A 1×1 transparent PNG — the mock's stand-in for a served profile image.
const TRANSPARENT_PNG = Uint8Array.from(
  atob(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/p8WAAAAAElFTkSuQmCC",
  ),
  (c) => c.charCodeAt(0),
);

// Seeded generated-image ids the GET /api/images/generated/{id} route serves (Wave
// G3). The chat's data: URI placeholders (client.ts MOCK_GENIMG) share these ids
// so a generate card and an edit card both render — and round-trip — offline.
const GENERATED_IMAGES = new Set<string>([
  "mock-genimg-lighthouse",
  "mock-genimg-lighthouse-stormy",
]);

// The image-launcher gallery (Wave L1): the owner-only render list the screen
// lists and prepends to, newest first. Seeded with a varied spread (kinds, dims)
// so the masonry + meta have content; a generate/edit fabricates a row and
// unshifts it here. Its ids serve a placeholder PNG via the by-id route below.
let imageGallery: GeneratedImageOut[] = [];
let imageGallerySeq = 0;

function seedImageGallery(): GeneratedImageOut[] {
  const base = Date.parse("2026-06-24T16:57:00Z");
  const seeds: Array<Omit<GeneratedImageOut, "id" | "created_at">> = [
    {
      kind: "generate",
      prompt: "a slate-blue ceramic teapot on a linen cloth, soft window light",
      width: 1024,
      height: 1024,
      model: "qwen-image",
      seed: 418207733,
    },
    {
      kind: "edit",
      prompt: "turn the linen cloth deep forest green, keep the teapot unchanged",
      width: 1024,
      height: 1024,
      model: "qwen-image-edit",
      seed: 901338121,
    },
    {
      kind: "generate",
      prompt: "a rainy alley at night, neon reflections",
      width: 768,
      height: 1344,
      model: "qwen-image-lightning",
      seed: 77410023,
    },
    {
      kind: "generate",
      prompt: "a foggy mountain ridge at dawn",
      width: 1344,
      height: 768,
      model: "qwen-image",
      seed: 553201984,
    },
    {
      kind: "generate",
      prompt: "a single paper crane on dark wood",
      width: 896,
      height: 1152,
      model: "dreamshaper",
      seed: 12009654,
    },
    {
      kind: "edit",
      prompt: "make the sky a warm dusk",
      width: 1152,
      height: 896,
      model: "qwen-image-edit",
      seed: 644120097,
    },
  ];
  // Newest first: later seeds get older timestamps so index 0 reads as freshest.
  return seeds.map((s, i) => ({
    ...s,
    id: `mock-gallery-${imageGallerySeq++}`,
    created_at: new Date(base - i * 3_600_000).toISOString(),
  }));
}
imageGallery = seedImageGallery();
for (const g of imageGallery) GENERATED_IMAGES.add(g.id);

// Resolution multipliers + aspect base dims mirror the form's summary so a
// fabricated render's dims match what the screen previewed (the real service
// owns these; here they only need to be plausible + varied).
const MOCK_ASPECT: Record<string, [number, number]> = {
  square: [1024, 1024],
  portrait: [896, 1152],
  landscape: [1152, 896],
  tall: [768, 1344],
  wide: [1344, 768],
};
const MOCK_RESMUL: Record<string, number> = { small: 0.75, medium: 1, large: 1.4 };
const MOCK_GEN_MODEL: Record<string, string> = {
  dreamshaper: "dreamshaper",
  fast: "qwen-image-lightning",
  quality: "qwen-image",
};
const MOCK_EDIT_MODEL: Record<string, string> = {
  dreamshaper: "qwen-image-edit-lightning",
  fast: "qwen-image-edit-lightning",
  quality: "qwen-image-edit",
};

interface MockGenSpec {
  prompt?: string;
  speed?: string;
  aspect?: string;
  resolution?: string;
  seed?: number | null;
  sourceImageId?: string | null;
}

function mockDims(aspect: string, resolution: string): [number, number] {
  const [w, h] = MOCK_ASPECT[aspect] ?? [1024, 1024];
  const m = MOCK_RESMUL[resolution] ?? 1;
  return [Math.round((w * m) / 8) * 8, Math.round((h * m) / 8) * 8];
}

function recordRender(kind: "generate" | "edit", spec: MockGenSpec): GeneratedImageOut {
  const speed = spec.speed ?? "quality";
  let width = 1024;
  let height = 1024;
  if (kind === "generate") {
    [width, height] = mockDims(spec.aspect ?? "square", spec.resolution ?? "medium");
  } else if (spec.sourceImageId) {
    // Edit inherits the source's dims (aspect "matches source" on the form).
    const src = imageGallery.find((g) => g.id === spec.sourceImageId);
    if (src) {
      width = src.width;
      height = src.height;
    }
  }
  const out: GeneratedImageOut = {
    id: `mock-render-${imageGallerySeq++}`,
    kind,
    prompt: spec.prompt ?? "",
    width,
    height,
    model:
      kind === "edit"
        ? (MOCK_EDIT_MODEL[speed] ?? "qwen-image-edit")
        : (MOCK_GEN_MODEL[speed] ?? "qwen-image"),
    seed: spec.seed ?? Math.floor(Math.random() * 4_294_967_295),
    created_at: new Date().toISOString(),
  };
  imageGallery.unshift(out);
  GENERATED_IMAGES.add(out.id);
  return out;
}

const VALID_DOMAINS = new Set(["general", "health", "finance", "location"]);

// Fake passage search over the note fixtures: substring match per term, a
// literal <mark> around the first hit (exercising the UI's mark-splitting),
// and a rotating match badge. `degraded!` anywhere in the query flips the
// keyword-only degraded banner on.
function mockSearch(params: URLSearchParams): { degraded: boolean; results: SearchHit[] } {
  const rawQ = params.get("q") ?? "";
  const domain = params.get("domain");
  const limit = Number(params.get("limit") ?? "20");
  const degraded = rawQ.includes("degraded!");
  const q = rawQ.replace(/degraded!/g, "").trim();
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);

  const matches = notes.filter((n) => {
    if (domain && n.domain !== domain) return false;
    if (terms.length === 0) return true;
    const body = n.body.toLowerCase();
    return terms.some((t) => body.includes(t));
  });

  const badges: SearchMatch[] = ["semantic", "keyword", "both"];
  const noteHits = matches.slice(0, limit).map((n, i): SearchResult => {
    const term = terms.find((t) => n.body.toLowerCase().includes(t));
    const at = term ? n.body.toLowerCase().indexOf(term) : -1;
    const snippet =
      at >= 0 && term
        ? `${n.body.slice(Math.max(0, at - 60), at)}<mark>${n.body.slice(at, at + term.length)}</mark>${n.body.slice(at + term.length, at + term.length + 80)}`
        : n.body.slice(0, 140);
    const fromAttachment = n.attachments.length > 0 && i % 2 === 1;
    return {
      kind: "note",
      note_id: n.id,
      chunk_id: id("chunk"),
      snippet,
      match: degraded ? "keyword" : (badges[i % badges.length] ?? "keyword"),
      score: 1 - i * 0.07,
      domain: n.domain,
      destination: n.destination,
      created_at: n.created_at,
      body_preview: n.body.slice(0, 120),
      attachment_count: n.attachments.length,
      source_kind: fromAttachment ? "attachment" : "note",
      source_anchor: fromAttachment ? `${n.attachments[0]?.filename ?? "file"} · p.1` : null,
    };
  });

  // The wiki leg: an article whose title/blurb matches a term ranks above note
  // passages (an article usually out-answers a raw passage). Degraded = the
  // semantic leg is down, so wiki articles (index-embedding-ranked) drop out.
  const wikiHits: WikiSearchResult[] =
    degraded || terms.length === 0
      ? []
      : WIKI_INDEX.filter((a) => {
          if (domain && a.domain !== domain) return false;
          const hay = `${a.title} ${a.blurb}`.toLowerCase();
          return terms.some((t) => hay.includes(t));
        }).map((a, i): WikiSearchResult => {
          const hay = `${a.title} ${a.blurb}`;
          const term = terms.find((t) => hay.toLowerCase().includes(t)) ?? "";
          const at = hay.toLowerCase().indexOf(term);
          const snippet =
            at >= 0 && term
              ? `${hay.slice(Math.max(0, at - 40), at)}<mark>${hay.slice(at, at + term.length)}</mark>${hay.slice(at + term.length, at + term.length + 60)}`
              : a.blurb;
          return {
            kind: "wiki",
            article_id: a.id,
            title: a.title,
            blurb: a.blurb,
            entity_kind: a.kind,
            domain: a.domain,
            snippet,
            match: "semantic",
            score: 2 - i * 0.05,
          };
        });

  // Merge by score (wiki articles score above note passages — the headline
  // answer layer), then honor the same `limit` the real API applies.
  const results: SearchHit[] = [...wikiHits, ...noteHits]
    .sort((a, b) => b.score - a.score)
    .slice(0, limit);
  return { degraded, results };
}

// Phase 7 location (Devices tab): the device-summary states the tab must render
// — a live device, an offline one (last fix hours ago, low battery), and a
// revoked one (no key, never reported). Mutable so provision/rotate/revoke behave
// in `dev:mock`.
interface MockDevice {
  id: string;
  label: string;
  created_at: string;
  revoked: boolean;
  last_seen: string | null;
  battery_pct: number | null;
  connection: string | null;
  velocity_mps: number | null;
  fix_count: number;
}

const MOCK_DEVICES: MockDevice[] = [
  {
    id: "dev-1",
    label: "Jeff's phone",
    created_at: "2026-05-01T00:00:00Z",
    revoked: false,
    last_seen: new Date(Date.now() - 4 * 60_000).toISOString(),
    battery_pct: 76,
    connection: "wifi",
    velocity_mps: 13.4, // ~30 mph — traveling
    fix_count: 4821,
  },
  {
    id: "dev-2",
    label: "Celine's phone",
    created_at: "2026-05-03T00:00:00Z",
    revoked: false,
    last_seen: new Date(Date.now() - 9 * 3_600_000).toISOString(),
    battery_pct: 18,
    connection: "mobile",
    velocity_mps: 0.3, // below the cutoff — not traveling
    fix_count: 2110,
  },
  {
    id: "dev-3",
    label: "Old tablet",
    created_at: "2026-02-01T00:00:00Z",
    revoked: true,
    last_seen: null,
    battery_pct: null,
    connection: null,
    velocity_mps: null,
    fix_count: 0,
  },
];
let mockDeviceSeq = 4;

// Mirror the backend's liveness judgment on read: an active device quiet past the
// horizon (1h) is "silent". Derived here rather than stored so the mock ages like the
// real read — Celine's 9h-old phone shows the silent badge.
const SILENT_AFTER_MS = 60 * 60_000;
function deviceSummary(d: MockDevice): DeviceSummary {
  const ageMs =
    d.last_seen === null ? null : Math.max(0, Date.now() - new Date(d.last_seen).getTime());
  return {
    ...d,
    age_seconds: ageMs === null ? null : ageMs / 1000,
    silent: !d.revoked && ageMs !== null && ageMs > SILENT_AFTER_MS,
  };
}

// Phase 7 location (Timeline tab): geofence crossings, newest first, keyed to the
// mock device subjects so the feed resolves "<device> left/arrived at <place>".
const MOCK_TIMELINE = [
  {
    occurred_at: new Date(Date.now() - 35 * 60_000).toISOString(),
    subject_id: "dev-1",
    transition: "exit",
    place_entity_id: "place-office",
    place_name: "Office",
  },
  {
    occurred_at: new Date(Date.now() - 6 * 3_600_000).toISOString(),
    subject_id: "dev-2",
    transition: "enter",
    place_entity_id: "place-mom",
    place_name: "Mom's house",
  },
  {
    occurred_at: new Date(Date.now() - 9 * 3_600_000).toISOString(),
    subject_id: "dev-1",
    transition: "enter",
    place_entity_id: "place-office",
    place_name: "Office",
  },
  {
    occurred_at: new Date(Date.now() - 30 * 3_600_000).toISOString(),
    subject_id: "dev-1",
    transition: "exit",
    place_entity_id: "place-home",
    place_name: "Home",
  },
];

// Phase 7 location (Map tab): fence circles + a synthetic trail near them, so the
// self-rendered map previews Live / Trail / Heat without a backend.
const MOCK_PLACES = [
  {
    place_entity_id: "place-office",
    name: "Office",
    enabled: true,
    center: { lat: 40.001, lon: -74.001 },
    radius_m: 120,
    polygon: null,
  },
  {
    place_entity_id: "place-home",
    name: "Home",
    enabled: true,
    center: { lat: 39.997, lon: -74.004 },
    radius_m: 90,
    polygon: null,
  },
];

function mockTrail(): {
  captured_at: string;
  latitude: number;
  longitude: number;
  accuracy_m: number | null;
  battery_pct: number | null;
  velocity_mps: number | null;
  course_deg: number | null;
  acceleration_mps2: number | null;
  altitude_m: number | null;
}[] {
  const out = [];
  for (let i = 0; i < 40; i++) {
    // A walk from Home toward Office, dwelling at the Office end (heat cluster).
    const t = Math.min(1, i / 25);
    const dwell = i > 25 ? (i - 25) * 0.0001 : 0;
    const vel = i < 25 ? Math.round(13 * Math.sin((i / 25) * Math.PI) * 10) / 10 : 0.2;
    out.push({
      captured_at: new Date(Date.now() - (40 - i) * 6 * 60_000).toISOString(),
      latitude: 39.997 + 0.004 * t + (Math.random() - 0.5) * 0.0002 + dwell,
      longitude: -74.004 + 0.003 * t + (Math.random() - 0.5) * 0.0002,
      accuracy_m: 8,
      battery_pct: 80 - Math.floor(i / 4),
      // A speed hump (0 → fast → 0) on the way, then near-still at the dwell, so the
      // trail shows the full speed-colour ramp in the mock.
      velocity_mps: vel,
      course_deg: Math.round((40 + i * 6) % 360), // a gentle curve for the heading wheel
      acceleration_mps2: Math.round(Math.abs(Math.sin(i / 3)) * 25) / 10, // 0..2.5 m/s²
      altitude_m: 38 + Math.round(8 * Math.sin(i / 5)),
    });
  }
  return out;
}

// --- Code mode (jcode) fixtures (dev:mock) ---
interface MockJcodeSession {
  id: string;
  repo: string;
  branch: string;
  work_branch: string;
  status: string;
  title: string;
  archived: boolean;
  created_at: string;
  last_active_at: string;
}
const jcodeSessions: MockJcodeSession[] = [
  {
    id: "j1",
    repo: "github.com/jeffmhopkins/scratch-todo",
    branch: "main",
    work_branch: "jcode/spike",
    status: "ready",
    title: "",
    archived: false,
    created_at: "2026-06-25T12:00:00Z",
    last_active_at: new Date().toISOString(),
  },
  {
    id: "j2",
    repo: "github.com/jeffmhopkins/JBrain2",
    branch: "main",
    work_branch: "jcode/local-mode",
    status: "ready",
    title: "",
    archived: false,
    created_at: "2026-06-20T09:00:00Z",
    last_active_at: "2026-06-20T09:00:00Z",
  },
];
let jcodeN = 2;
let jcodeModelPolls = 0;
// The coder isn't on the box until the owner confirms the swap (POST /jcode/model/warm);
// before that the screen shows the load prompt naming what gets evicted.
let jcodeWarmStarted = false;
// The master power switch (dev:mock): on by default; toggling it flips the service states
// the launcher's switch + modal read back.
let jcodePowerOn = true;
const jcodePreview = new Map<string, string>();
// Per-session share links the owner has minted (dev:mock), so the share manager UI has
// something to list and revoke.
const jcodeShares: Record<
  string,
  {
    id: string;
    label: string;
    created_at: string;
    expires_at: string | null;
    last_used_at: string | null;
    redeemed_at: string | null;
  }[]
> = {};
let jcodeShareN = 1;
// External-LLM endpoints the owner has minted (dev:mock), so the launcher's external
// section + the external screen have something to list, toggle, and revoke.
const jcodeExternal: {
  id: string;
  label: string;
  enabled: boolean;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  in_tokens: number;
  out_tokens: number;
  requests: number;
}[] = [];
let jcodeExternalN = 1;

// --- JPet (the wall pet): one in-memory authoritative state the mock mutates (v2) ---
const MOCK_OBJECTS: Record<string, [number, number]> = {
  ball: [0, 0.35],
  bed: [-0.72, -0.7],
  toy_box: [0.72, -0.7],
  food_bowl: [0.72, 0.72],
  ball_pit: [-0.72, 0.72],
  light_switch: [0, -0.92],
};
const MOCK_LOCATIONS: Record<string, [number, number]> = {
  corner_ne: [0.85, -0.85],
  corner_nw: [-0.85, -0.85],
  corner_se: [0.85, 0.85],
  corner_sw: [-0.85, 0.85],
  center: [0, 0],
  near_child: [0, 0.82],
};
// The canned kid-button scripts, mirrored from the backend so dev:mock plays them out.
const MOCK_SCRIPTS: Record<string, PetState["script"]> = {
  dance: [{ action: "dance", duration_ms: 2200, emotion: "silly" }, { action: "sit" }],
  spin: [{ action: "spin", duration_ms: 1400, emotion: "excited" }, { action: "idle" }],
  jump: [{ action: "jump", duration_ms: 900, emotion: "excited" }, { action: "idle" }],
  wave: [{ action: "wave", duration_ms: 1200, emotion: "happy" }, { action: "idle" }],
  wiggle: [{ action: "wiggle", duration_ms: 1400, emotion: "silly" }, { action: "idle" }],
  chase: [
    { action: "chase", target: "ball", emotion: "excited" },
    { action: "wiggle", duration_ms: 900 },
    { action: "sit" },
  ],
  hide: [{ action: "hide", destination: "corner_nw", emotion: "curious" }, { action: "sit" }],
  beep: [{ action: "beep", duration_ms: 700, emotion: "silly" }, { action: "idle" }],
  come: [
    { action: "come_here", emotion: "happy" },
    { action: "wave", duration_ms: 900 },
    { action: "idle" },
  ],
  sleep: [{ action: "go_to", target: "bed", emotion: "sleepy" }, { action: "sleep" }],
  wake: [
    { action: "wake", emotion: "happy" },
    { action: "wiggle", duration_ms: 800 },
    { action: "idle" },
  ],
  eat: [
    { action: "go_to", target: "food_bowl", emotion: "happy" },
    { action: "nod", duration_ms: 1200 },
    { action: "sit" },
  ],
  lights: [
    { action: "go_to", target: "light_switch", emotion: "curious" },
    { action: "jump", duration_ms: 700 },
    { action: "idle" },
  ],
  jumprope: [{ action: "jumprope", duration_ms: 3000, emotion: "excited" }, { action: "idle" }],
  music: [{ action: "play_music", duration_ms: 3000, emotion: "silly" }, { action: "idle" }],
  sing: [{ action: "sing", duration_ms: 6000, emotion: "silly" }, { action: "idle" }],
  fart: [{ action: "fart", duration_ms: 1400, emotion: "silly" }, { action: "idle" }],
  burp: [{ action: "burp", duration_ms: 1400, emotion: "silly" }, { action: "idle" }],
};

const mockPet: PetState = {
  name: "Blink",
  domain: "general",
  mood: "playful",
  emotion: "happy",
  speech: null,
  asleep: false,
  pos_x: 0,
  pos_z: 0.2,
  target_x: 0,
  target_z: 0.2,
  facing: 0,
  action: "idle",
  color: null,
  script: [],
  carrying: null,
  lights_on: true,
  objects: { ...MOCK_OBJECTS },
};

// Colour names the mock accepts (mirror of the backend `PET_COLORS`), for the palette.
const MOCK_COLORS = [
  "cyan",
  "magenta",
  "gold",
  "orange",
  "blue",
  "red",
  "green",
  "pink",
  "purple",
  "white",
  "rainbow",
  "default",
];

// A compact mirror of the backend's settle_script: fold a script into the pet + room's
// resting state so the mock stays coherent (carry the ball, toggle lights, nap).
function mockSettle(script: PetState["script"]): void {
  const clampUnit = (n: number) => Math.max(-1, Math.min(1, n));
  let pos: [number, number] = [mockPet.pos_x, mockPet.pos_z];
  let held = mockPet.carrying;
  for (const step of script) {
    const dest = step.destination ? MOCK_LOCATIONS[step.destination] : undefined;
    const tgt = step.target ? mockPet.objects[step.target] : undefined;
    let want: [number, number] | undefined = dest ?? undefined;
    if (!want && ["go_to", "look_at", "chase", "pick_up"].includes(step.action) && tgt) want = tgt;
    if (!want && step.action === "chase") want = mockPet.objects.ball;
    if (!want && step.action === "come_here") want = MOCK_LOCATIONS.near_child;
    if (!want && step.action === "hide") want = MOCK_LOCATIONS.corner_nw;
    if (want) pos = [clampUnit(want[0]), clampUnit(want[1])];
    if (step.action === "pick_up") held = step.target ?? "ball";
    else if (step.action === "put_down") held = null;
    if (step.action === "go_to" && step.target === "light_switch")
      mockPet.lights_on = !mockPet.lights_on;
    if (held) mockPet.objects[held] = [pos[0], pos[1]];
    if (step.action === "sleep") mockPet.asleep = true;
    else if (step.action === "wake") mockPet.asleep = false;
  }
  mockPet.pos_x = pos[0];
  mockPet.pos_z = pos[1];
  mockPet.target_x = pos[0];
  mockPet.target_z = pos[1];
  mockPet.carrying = held;
  const last = script[script.length - 1];
  mockPet.action = last ? last.action : "idle";
}

function applyMockPetCommand(command: PetCommand): void {
  if (command.action === "move") {
    mockPet.target_x = command.x ?? mockPet.target_x;
    mockPet.target_z = command.z ?? mockPet.target_z;
    mockPet.action = "walk";
    mockPet.asleep = false;
    mockPet.script = [];
    return;
  }
  if (command.action === "color") {
    const c = (command.text ?? "").trim().toLowerCase();
    mockPet.color = MOCK_COLORS.includes(c) ? c : "rainbow";
    mockPet.speech = mockPet.color === "rainbow" ? "Rainbow time!" : `Ooh, ${mockPet.color}!`;
    return;
  }
  if (command.action === "say") {
    const said = (command.text ?? "").trim().toLowerCase();
    // Mirror the backend's fast classifier: a colour word in the message recolours.
    const spoken = MOCK_COLORS.find((c) => said.includes(c));
    if (spoken) {
      mockPet.color = spoken;
      mockPet.speech = spoken === "rainbow" ? "Rainbow time!" : `Ooh, ${spoken}!`;
      return;
    }
    // Mirror the backend chat layer: common small talk gets a funny reply + emote.
    const chat: [string[], string][] = [
      [["how are you", "how do you feel"], "Sooo happy and wiggly, thank you! How are YOU?"],
      [["love you", "love oo"], "I love oo too! Squishy robot hug! Mwah!"],
      [
        ["joke", "knock knock", "funny"],
        "Why did the robot boogie? To shake its bolts off! Hee-hee!",
      ],
      [["your name", "who are you", "what are you"], "I'm your silly robot buddy! Beep-boop-beep!"],
      [["thank you", "thanks"], "You're welcome, best friend!"],
      [["what are you doing", "whatcha doing"], "Playing playing playing! Wanna play?"],
      [["bye", "goodbye", "see you"], "Bye bye! Come play again super soon!"],
    ];
    const hit = chat.find(([keys]) => keys.some((k) => said.includes(k)));
    const babble = [
      "Hee-hee! Boop boop!",
      "Beeble-beeble-boop! That tickles my circuits!",
      "Ooh ooh, tell me more! Boop!",
      "Dah-boo! You're my favourite!",
      "Bzzt-bloop! Robots love chatting!",
      "Beep? Beep beep! (that means yay!)",
    ];
    mockPet.speech = hit ? hit[1] : (babble[Math.floor(Math.random() * babble.length)] ?? "Boo!");
    mockPet.emotion = "excited";
    // Playful stand-in: acting out freeform speech isn't parsed in the mock, so wiggle.
    mockPet.script = [{ action: "wiggle", duration_ms: 1200 }, { action: "idle" }];
    mockSettle(mockPet.script);
    return;
  }
  const script = MOCK_SCRIPTS[command.action] ?? [{ action: "idle" }];
  mockPet.script = script;
  mockPet.emotion = "excited";
  mockSettle(script);
  mockPet.mood = mockPet.asleep ? "sleepy" : "playful";
}

export const mockFetch: typeof fetch = async (input, init) => {
  await sleep();
  const url = new URL(String(input instanceof Request ? input.url : input), "http://mock");
  const path = url.pathname;
  const method = (init?.method ?? "GET").toUpperCase();

  if (path === "/api/auth/session") return new Response(null, { status: 204 });
  if (path === "/api/auth/me") return json(PRINCIPAL);

  // --- JPet (the wall pet) ---
  if (path === "/api/pet" && method === "GET") return json(mockPet);
  if (path === "/api/pet/command" && method === "POST") {
    applyMockPetCommand(JSON.parse(String(init?.body ?? "{}")) as PetCommand);
    return json(mockPet);
  }
  if (path === "/api/pet/stream" && method === "GET") {
    const frame = `data: ${JSON.stringify(mockPet)}\n\n`;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(frame));
        // leave open: a real stream stays subscribed; the caller aborts on unmount
      },
    });
    return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
  }

  // Chat composer capabilities: vision gate + the agent.turn window, so the context
  // meter can seed a fresh chat before the first turn reports live usage.
  if (path === "/api/chat/capabilities" && method === "GET") {
    return json({ supports_vision: true, can_edit_images: false, context_window: 262144 });
  }

  // --- Code mode (jcode) ---
  // Model status: until the owner confirms the swap, report the coder absent with another
  // model resident (the screen shows the load prompt). After the warm POST, report
  // "loading" for a couple of polls, then resident — so the loading bar advances, then
  // completes.
  if (path === "/api/jcode/model" && method === "GET") {
    if (!jcodeWarmStarted) {
      return json({
        model: "qwen3-coder-next",
        served: "qwen3-coder-next",
        loaded: false,
        warming: false,
        progress: null,
        hosting: true,
        size_gb: 49.6,
        context_window: 262144,
        resident: ["gpt-oss-120b"],
      });
    }
    jcodeModelPolls += 1;
    const ready = jcodeModelPolls > 2;
    return json({
      model: "qwen3-coder-next",
      served: "qwen3-coder-next",
      loaded: ready,
      warming: !ready,
      // Climb the real load fraction over the polls so the bar tracks it, then settle.
      progress: ready ? 1 : Math.min(0.9, jcodeModelPolls * 0.35),
      hosting: true,
      size_gb: 49.6,
      context_window: 262144,
      resident: ready ? ["qwen3-coder-next"] : [],
    });
  }
  if (path === "/api/jcode/model/warm" && method === "POST") {
    jcodeWarmStarted = true;
    jcodeModelPolls = 0;
    return json({
      model: "qwen3-coder-next",
      served: "qwen3-coder-next",
      loaded: false,
      warming: true,
      progress: 0,
      hosting: true,
      size_gb: 49.6,
      context_window: 262144,
      resident: [],
    });
  }
  // Master power switch: report/toggle the jcode-only services + coder residency.
  if (path === "/api/jcode/power" && (method === "GET" || method === "POST")) {
    if (method === "POST") {
      const pbody = JSON.parse(String(init?.body)) as { on?: boolean };
      jcodePowerOn = pbody.on ?? jcodePowerOn;
      if (!jcodePowerOn) jcodeWarmStarted = false; // powering off unloads the coder
    }
    const services = ["local-llm", "claude-shim", "jcode"].map((name) => ({
      name,
      running: jcodePowerOn,
    }));
    return json({
      on: jcodePowerOn,
      provisioned: true,
      services,
      coder_loaded: jcodePowerOn && jcodeWarmStarted && jcodeModelPolls > 2,
      warming: jcodePowerOn && jcodeWarmStarted && jcodeModelPolls <= 2,
      model: "qwen3-coder-next",
      size_gb: 49.6,
      hosting: true,
      live_sessions: jcodePowerOn ? jcodeSessions.filter((s) => s.status !== "stopped").length : 0,
    });
  }
  if (path === "/api/jcode/sessions" && method === "GET") return json(jcodeSessions);
  if (path === "/api/jcode/sessions" && method === "POST") {
    const body = JSON.parse(String(init?.body)) as {
      repo: string;
      branch: string;
      work_branch: string;
    };
    jcodeN += 1;
    const id = `j${jcodeN}`;
    const now = new Date().toISOString();
    const s: MockJcodeSession = {
      id,
      repo: body.repo,
      branch: body.branch || "main",
      work_branch: body.work_branch || `jcode/${id}`,
      status: "ready",
      title: "",
      archived: false,
      created_at: now,
      last_active_at: now,
    };
    jcodeSessions.unshift(s);
    return json(s, 201);
  }
  // Rename / archive / unarchive — the launcher's swipe-rail actions (owner-only).
  if (path.startsWith("/api/jcode/sessions/") && path.endsWith("/archive") && method === "POST") {
    const s = jcodeSessions.find((x) => x.id === path.split("/")[4]);
    if (s) s.archived = true;
    return new Response(null, { status: 204 });
  }
  if (path.startsWith("/api/jcode/sessions/") && path.endsWith("/unarchive") && method === "POST") {
    const s = jcodeSessions.find((x) => x.id === path.split("/")[4]);
    if (s) s.archived = false;
    return new Response(null, { status: 204 });
  }
  if (path.startsWith("/api/jcode/sessions/") && method === "PATCH") {
    const s = jcodeSessions.find((x) => x.id === path.split("/")[4]);
    const body = JSON.parse(String(init?.body)) as { title?: string };
    if (s) s.title = body.title ?? "";
    return new Response(null, { status: 204 });
  }
  if (path.startsWith("/api/jcode/sessions/") && path.endsWith("/reset") && method === "POST") {
    const s = jcodeSessions.find((x) => x.id === path.split("/")[4]);
    return s ? json(s) : json({ detail: "unknown session" }, 404);
  }
  // Stop pauses (keeps the checkout); restart resumes — the terminal-exit lifecycle.
  if (path.startsWith("/api/jcode/sessions/") && path.endsWith("/stop") && method === "POST") {
    const s = jcodeSessions.find((x) => x.id === path.split("/")[4]);
    if (s) s.status = "stopped";
    return s ? json(s) : json({ detail: "unknown session" }, 404);
  }
  if (path.startsWith("/api/jcode/sessions/") && path.endsWith("/restart") && method === "POST") {
    const s = jcodeSessions.find((x) => x.id === path.split("/")[4]);
    if (s) s.status = "ready";
    return s ? json(s) : json({ detail: "unknown session" }, 404);
  }
  // Share-link routes (owner mint/list/revoke) — before the session DELETE so the
  // revoke DELETE on /shares/{id} doesn't get swallowed by the session DELETE below.
  if (path.startsWith("/api/jcode/sessions/") && path.endsWith("/share") && method === "POST") {
    const sid = path.split("/")[4] ?? "";
    const body = JSON.parse(String(init?.body)) as { label?: string; ttl_hours?: number };
    const id = `share-${jcodeShareN++}`;
    const expires_at = new Date(Date.now() + (body.ttl_hours ?? 24) * 3600_000).toISOString();
    const share = {
      id,
      label: body.label || "shared link",
      created_at: new Date().toISOString(),
      expires_at,
      last_used_at: null,
      redeemed_at: null,
    };
    jcodeShares[sid] = jcodeShares[sid] ?? [];
    jcodeShares[sid].unshift(share);
    return json({ id, label: share.label, expires_at, token: `mock-share-secret-${id}` }, 201);
  }
  if (path.startsWith("/api/jcode/sessions/") && path.endsWith("/shares") && method === "GET") {
    return json(jcodeShares[path.split("/")[4] ?? ""] ?? []);
  }
  if (path.includes("/shares/") && method === "DELETE") {
    const parts = path.split("/");
    const sid = parts[4] ?? "";
    const shareId = parts[6] ?? "";
    const list = jcodeShares[sid] ?? [];
    const i = list.findIndex((s) => s.id === shareId);
    if (i < 0) return json({ detail: "unknown share link" }, 404);
    list.splice(i, 1);
    return new Response(null, { status: 204 });
  }
  if (path === "/api/jcode/share/redeem" && method === "POST") {
    return json({ session_id: jcodeSessions[0]?.id ?? "j1" });
  }

  // External-LLM sessions (owner): mint/list/toggle/revoke a public coder endpoint.
  if (path === "/api/jcode/external" && method === "GET") return json(jcodeExternal);
  if (path === "/api/jcode/external" && method === "POST") {
    const body = JSON.parse(String(init?.body)) as { label?: string; ttl_hours?: number };
    const id = `ext-${jcodeExternalN++}`;
    const expires_at = body.ttl_hours
      ? new Date(Date.now() + body.ttl_hours * 3600_000).toISOString()
      : null;
    jcodeExternal.unshift({
      id,
      label: body.label || "external session",
      enabled: true,
      created_at: new Date().toISOString(),
      expires_at,
      last_used_at: null,
      in_tokens: 0,
      out_tokens: 0,
      requests: 0,
    });
    return json(
      {
        id,
        label: body.label || "external session",
        expires_at,
        token: `mock-ext-secret-${id}`,
        url: `${window.location.origin}/api/ext/llm/${id}`,
      },
      201,
    );
  }
  if (path.startsWith("/api/jcode/external/") && path.endsWith("/enabled") && method === "POST") {
    const id = path.split("/")[4] ?? "";
    const body = JSON.parse(String(init?.body)) as { enabled: boolean };
    const s = jcodeExternal.find((x) => x.id === id);
    if (s) s.enabled = body.enabled;
    return json({ enabled: body.enabled });
  }
  if (path.startsWith("/api/jcode/external/") && method === "DELETE") {
    const id = path.split("/")[4] ?? "";
    const i = jcodeExternal.findIndex((x) => x.id === id);
    if (i >= 0) jcodeExternal.splice(i, 1);
    return new Response(null, { status: 204 });
  }
  // Preview routes first — the DELETE here would otherwise match the session DELETE.
  if (path.startsWith("/api/jcode/sessions/") && path.endsWith("/preview")) {
    const sid = path.split("/")[4] ?? "";
    if (method === "GET") return json({ enabled: true, url: jcodePreview.get(sid) ?? null });
    if (method === "POST") {
      const url = `https://demo-${sid}.trycloudflare.com`;
      jcodePreview.set(sid, url);
      return json({ enabled: true, url });
    }
    if (method === "DELETE") {
      jcodePreview.delete(sid);
      return new Response(null, { status: 204 });
    }
  }
  if (path.startsWith("/api/jcode/sessions/") && method === "DELETE") {
    const i = jcodeSessions.findIndex((x) => x.id === path.split("/")[4]);
    if (i >= 0) jcodeSessions.splice(i, 1);
    return new Response(null, { status: 204 });
  }
  if (path.startsWith("/api/jcode/runs/") && path.endsWith("/cancel") && method === "POST") {
    return new Response(null, { status: 202 });
  }

  if (path === "/api/notes" && method === "GET") {
    const limit = Number(url.searchParams.get("limit") ?? "50");
    const before = url.searchParams.get("before");
    // The stream excludes hidden notes (they remain in Search).
    let pool = notes
      .filter((n) => !n.hidden)
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
    if (before) pool = pool.filter((n) => n.created_at < before);
    const page = pool.slice(0, limit);
    const last = page[page.length - 1];
    return json({ notes: page, next_cursor: pool.length > limit && last ? last.created_at : null });
  }

  if (path === "/api/notes" && method === "POST") {
    const body = JSON.parse(String(init?.body)) as {
      client_id: string;
      domain?: string;
      destination?: string | null;
      body: string;
      latitude?: number;
      longitude?: number;
      accuracy_m?: number;
    };
    const domain = body.domain ?? "general";
    if (!VALID_DOMAINS.has(domain)) return json({ detail: "unknown domain" }, 400);
    const existing = notes.find((n) => n.client_id === body.client_id);
    if (existing) return json(existing, 201);
    const note = seedNote(
      domain,
      body.destination ?? null,
      body.body,
      new Date().toISOString(),
      [],
      "pending",
    );
    note.client_id = body.client_id;
    note.latitude = body.latitude ?? null;
    note.longitude = body.longitude ?? null;
    note.accuracy_m = body.accuracy_m ?? null;
    notes.push(note);
    return json(note, 201);
  }

  const noteMatch = path.match(/^\/api\/notes\/([^/]+)$/);
  if (noteMatch && method === "PATCH") {
    const note = notes.find((n) => n.id === decodeURIComponent(noteMatch[1] ?? ""));
    if (!note) return json({ detail: "note not found" }, 404);
    const patch = JSON.parse(String(init?.body)) as {
      body?: string;
      domain?: string;
      destination?: string | null;
    };
    if (patch.domain !== undefined && !VALID_DOMAINS.has(patch.domain))
      return json({ detail: "unknown domain" }, 400);
    if (patch.body !== undefined) note.body = patch.body;
    if (patch.domain !== undefined) note.domain = patch.domain;
    if ("destination" in patch) note.destination = patch.destination ?? null;
    // Mirrors the real PATCH: edits re-trigger ingestion.
    note.ingest_state = "pending";
    return json(note);
  }

  if (noteMatch && method === "DELETE") {
    const noteId = decodeURIComponent(noteMatch[1] ?? "");
    const index = notes.findIndex((n) => n.id === noteId);
    if (index < 0) return json({ detail: "note not found" }, 404);
    notes.splice(index, 1);
    // Mirror the backend purge: review items derived from the note go too
    // (any status), as does its analysis fixture.
    for (let i = REVIEW_ITEMS.length - 1; i >= 0; i--) {
      if (REVIEW_ITEMS[i]?.payload.note_id === noteId) REVIEW_ITEMS.splice(i, 1);
    }
    delete ANALYSES[noteId];
    return new Response(null, { status: 204 });
  }

  const hideMatch = path.match(/^\/api\/notes\/([^/]+)\/(hide|unhide)$/);
  if (hideMatch && method === "POST") {
    const note = notes.find((n) => n.id === decodeURIComponent(hideMatch[1] ?? ""));
    if (!note) return json({ detail: "note not found" }, 404);
    note.hidden = hideMatch[2] === "hide";
    return new Response(null, { status: 204 });
  }

  const noteAnalyzeMatch = path.match(/^\/api\/notes\/([^/]+)\/analyze$/);
  if (noteAnalyzeMatch && method === "POST") {
    const noteId = decodeURIComponent(noteAnalyzeMatch[1] ?? "");
    const note = notes.find((n) => n.id === noteId);
    if (!note) return json({ detail: "note not found" }, 404);
    if (analyzingNotes.has(noteId)) {
      return json({ detail: "analysis already queued or running" }, 409);
    }
    // The re-run walks the real gated sequence: analyzed drops right away,
    // image extracts land first (the gate), then the analysis row upserts
    // with a bumped analyzed_at the tab's poller can see.
    analyzingNotes.add(noteId);
    note.analyzed = false;
    setTimeout(() => {
      analyzingNotes.delete(noteId);
      const live = notes.find((n) => n.id === noteId);
      if (!live) return;
      for (const att of live.attachments) {
        if (!att.media_type.startsWith("image/")) continue;
        if (!attachmentExtracts.has(att.id)) attachmentExtracts.set(att.id, extractFixtures());
        att.has_extracts = true;
        att.has_description = true;
      }
      live.analyzed = true;
      upsertAnalysis(noteId);
    }, LATENCY_MS * 4);
    return json({ job_id: id("job") }, 202);
  }

  if (path === "/api/search" && method === "GET") {
    return json(mockSearch(url.searchParams));
  }

  const attachMatch = path.match(/^\/api\/notes\/([^/]+)\/attachments$/);
  if (attachMatch && method === "POST") {
    const note = notes.find((n) => n.id === decodeURIComponent(attachMatch[1] ?? ""));
    if (!note) return json({ detail: "unknown note" }, 404);
    const file = init?.body instanceof FormData ? init.body.get("file") : null;
    if (!(file instanceof Blob)) return json({ detail: "missing file" }, 422);
    const filename = file instanceof File ? file.name : "upload.bin";
    const att: AttachmentOut = {
      id: id("att"),
      filename,
      media_type: file.type || "application/octet-stream",
      size_bytes: file.size,
      has_extracts: false, // fresh uploads always start pre-OCR
      has_description: false,
    };
    attachmentBlobs.set(att.id, file);
    note.attachments.push(att);
    return json(att, 201);
  }

  const extractsMatch = path.match(/^\/api\/attachments\/([^/]+)\/extracts$/);
  if (extractsMatch && method === "GET") {
    const attId = decodeURIComponent(extractsMatch[1] ?? "");
    const known = notes.some((n) => n.attachments.some((a) => a.id === attId));
    if (!known) return json({ detail: "attachment not found" }, 404);
    return json({ extracts: attachmentExtracts.get(attId) ?? [] });
  }

  const analyzeMatch = path.match(/^\/api\/attachments\/([^/]+)\/analyze$/);
  if (analyzeMatch && method === "POST") {
    const attId = decodeURIComponent(analyzeMatch[1] ?? "");
    const att = notes.flatMap((n) => n.attachments).find((a) => a.id === attId);
    if (!att) return json({ detail: "attachment not found" }, 404);
    if (analyzingAttachments.has(attId)) {
      return json({ detail: "analysis already queued or running" }, 409);
    }
    // A tick later the fixture flips like the worker would: OCR if missing,
    // a fresh description, and the chip signals on the attachment row.
    analyzingAttachments.add(attId);
    setTimeout(() => {
      analyzingAttachments.delete(attId);
      const fresh = extractFixtures();
      const existing = attachmentExtracts.get(attId) ?? [];
      const kept = existing.filter((e) => e.kind === "ocr");
      attachmentExtracts.set(attId, [
        ...(kept.length > 0 ? kept : fresh.filter((e) => e.kind === "ocr")),
        ...fresh.filter((e) => e.kind === "caption"),
      ]);
      att.has_extracts = true;
      att.has_description = true;
      // The gate: once every image on the note has extracts, analysis lands
      // too — the whiteboard fixture round-trips the gated sequence.
      const owner = notes.find((n) => n.attachments.some((a) => a.id === attId));
      if (owner?.attachments.every((a) => !a.media_type.startsWith("image/") || a.has_extracts)) {
        owner.analyzed = true;
        upsertAnalysis(owner.id);
      }
    }, LATENCY_MS * 3);
    return json({ job_id: id("job") }, 202);
  }

  // The on-box piper TTS, proxied through the api (read-aloud voice picker + samples).
  if (path === "/api/brain/voices" && method === "GET") return json({ voices: BRAIN_VOICES });
  if (path === "/api/brain/speakers" && method === "GET") return json({ speakers: BRAIN_SPEAKERS });
  if (path.startsWith("/api/brain/tts") && method === "GET") {
    // A tiny empty WAV stand-in — enough for a same-origin <audio> load in dev; tests
    // stub the api layer directly rather than exercising this.
    return new Response(new Blob([], { type: "audio/wav" }), {
      headers: { "Content-Type": "audio/wav" },
    });
  }

  if (path === "/api/settings" && method === "GET") return json(SETTINGS);
  if (path === "/api/settings" && method === "PUT") {
    const patch = JSON.parse(String(init?.body)) as Record<string, unknown>;
    // Mirror the backend's strict validation: unknown keys/values are 422s.
    for (const [key, value] of Object.entries(patch)) {
      if (key === "image_analysis_mode") {
        if (value !== "full" && value !== "ocr") return json({ detail: "unknown mode" }, 422);
        SETTINGS.image_analysis_mode = value;
      } else if (key === "owner_timezone") {
        if (typeof value !== "string") return json({ detail: "bad timezone" }, 422);
        SETTINGS.owner_timezone = value;
      } else if (key === "brain_llm_stream") {
        if (typeof value !== "boolean") return json({ detail: "bad brain_llm_stream" }, 422);
        SETTINGS.brain_llm_stream = value;
      } else if (key === "brain_read_aloud") {
        if (typeof value !== "boolean") return json({ detail: "bad brain_read_aloud" }, 422);
        SETTINGS.brain_read_aloud = value;
      } else if (key === "brain_answer_voice") {
        if (typeof value !== "string" || !value.trim()) return json({ detail: "empty voice" }, 422);
        SETTINGS.brain_answer_voice = value;
      } else if (key === "brain_read_aloud_engine") {
        if (value !== "piper" && value !== "native") return json({ detail: "bad engine" }, 422);
        SETTINGS.brain_read_aloud_engine = value;
      } else {
        return json({ detail: `unknown key ${key}` }, 422);
      }
    }
    return json(SETTINGS);
  }

  // The on-box image service (ComfyUI) surfaced in the LLM drawer.
  if (path === "/api/settings/image" && method === "GET") return json(IMAGE_SETTINGS);
  if (path === "/api/settings/image/free" && method === "POST") {
    // Freeing unloads the resident model: VRAM returns to fully free.
    IMAGE_SETTINGS.memory = { total_gb: 128, free_gb: 128 };
    return json(IMAGE_SETTINGS);
  }
  if (path === "/api/settings/image/service/start" && method === "POST") {
    IMAGE_SETTINGS.reachable = true;
    IMAGE_SETTINGS.memory = { total_gb: 128, free_gb: 128 };
    return new Response(JSON.stringify({ service: "comfyui", action: "start" }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (path === "/api/settings/image/service/stop" && method === "POST") {
    IMAGE_SETTINGS.reachable = false;
    IMAGE_SETTINGS.memory = null;
    return new Response(JSON.stringify({ service: "comfyui", action: "stop" }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    });
  }
  // The composer's Stop: cancel the detached chat turn. Chat itself isn't mocked,
  // but the route exists (and is idempotent) so the call never 404s in mock dev.
  if (/^\/api\/chat\/runs\/[^/]+\/cancel$/.test(path) && method === "POST") {
    return new Response(null, { status: 204 });
  }
  // The chat "Stop render" control (the live image preview's Stop). Chat itself
  // isn't mocked, but the route exists so the call never 404s in mock dev.
  if (path === "/api/settings/image/interrupt" && method === "POST") {
    return new Response(JSON.stringify({ status: "interrupted" }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (path === "/api/settings/llm" && method === "GET") return json(LLM_SETTINGS);
  if (path === "/api/settings/llm" && method === "PUT") {
    const body = JSON.parse(String(init?.body)) as {
      tasks: Record<string, { provider: LlmProviderId; reasoning_effort?: ReasoningEffort }>;
    };
    for (const [taskId, patch] of Object.entries(body.tasks)) applyLlmPatch(taskId, patch);
    return json(LLM_SETTINGS);
  }
  if (path === "/api/settings/llm/jcode-model" && method === "PUT") {
    const body = JSON.parse(String(init?.body)) as { model: string };
    // Mirror the API: "" reverts to the default; else it must be an offered option.
    const valid = body.model === "" || LLM_SETTINGS.jcode.options.some((o) => o.id === body.model);
    if (!valid) return new Response("model must be installed + tool-capable", { status: 422 });
    LLM_SETTINGS.jcode.model = body.model || LLM_SETTINGS.jcode.default;
    return json(LLM_SETTINGS);
  }
  if (path === "/api/settings/llm/jcode-planner" && method === "PUT") {
    const body = JSON.parse(String(init?.body)) as { planner: string };
    // Mirror the API: "" reverts to the split default; the "same" sentinel is single-model;
    // else it must be an offered option.
    const valid =
      body.planner === "" ||
      body.planner === LLM_SETTINGS.jcode.planner_same ||
      LLM_SETTINGS.jcode.options.some((o) => o.id === body.planner);
    if (!valid) return new Response("planner must be installed + tool-capable", { status: 422 });
    LLM_SETTINGS.jcode.planner = body.planner || LLM_SETTINGS.jcode.planner_default;
    return json(LLM_SETTINGS);
  }
  const unloadMatch = path.match(/^\/api\/settings\/llm\/local-models\/(.+)\/unload$/);
  if (unloadMatch && method === "POST") {
    const id = decodeURIComponent(unloadMatch[1] ?? "");
    const model = LLM_SETTINGS.local_models.find((m) => m.id === id);
    if (model) model.loaded = false;
    return json({
      loaded: LLM_SETTINGS.local_models.filter((m) => m.loaded).map((m) => m.id),
      reachable: true,
    });
  }
  const loadMatch = path.match(/^\/api\/settings\/llm\/local-models\/(.+)\/load$/);
  if (loadMatch && method === "POST") {
    const id = decodeURIComponent(loadMatch[1] ?? "");
    const model = LLM_SETTINGS.local_models.find((m) => m.id === id);
    if (model) {
      model.loaded = true;
      model.staged = false;
    }
    return json({
      loaded: LLM_SETTINGS.local_models.filter((m) => m.loaded).map((m) => m.id),
      reachable: true,
    });
  }
  const stageMatch = path.match(/^\/api\/settings\/llm\/local-models\/(.+)\/stage$/);
  if (stageMatch && (method === "POST" || method === "DELETE")) {
    const id = decodeURIComponent(stageMatch[1] ?? "");
    const model = LLM_SETTINGS.local_models.find((m) => m.id === id);
    if (model) model.staged = method === "POST";
    return json(LLM_SETTINGS);
  }
  const installMatch = path.match(/^\/api\/settings\/llm\/local-models\/(.+)\/install$/);
  if (installMatch && (method === "POST" || method === "DELETE")) {
    const id = decodeURIComponent(installMatch[1] ?? "");
    const model = LLM_SETTINGS.local_models.find((m) => m.id === id);
    if (model && !model.enabled) model.queued = method === "POST";
    return json(LLM_SETTINGS);
  }
  const uninstallMatch = path.match(/^\/api\/settings\/llm\/local-models\/(.+)\/uninstall$/);
  if (uninstallMatch && (method === "POST" || method === "DELETE")) {
    const id = decodeURIComponent(uninstallMatch[1] ?? "");
    const model = LLM_SETTINGS.local_models.find((m) => m.id === id);
    if (model?.enabled) model.remove_queued = method === "POST";
    return json(LLM_SETTINGS);
  }
  const windowMatch = path.match(/^\/api\/settings\/llm\/local-models\/(.+)\/context-window$/);
  if (windowMatch && method === "PUT") {
    const id = decodeURIComponent(windowMatch[1] ?? "");
    const model = LLM_SETTINGS.local_models.find((m) => m.id === id);
    const body = JSON.parse(String(init?.body)) as { context_window: number | null };
    if (model) {
      model.context_window_override = body.context_window;
      const eff = body.context_window ?? model.context_window;
      model.kv_gb =
        Math.round((model.kv_gb / model.context_window) * eff * 100) / 100 || model.kv_gb;
    }
    return json(LLM_SETTINGS);
  }

  const blobMatch = path.match(/^\/api\/attachments\/([^/]+)$/);
  if (blobMatch && method === "DELETE") {
    const attId = decodeURIComponent(blobMatch[1] ?? "");
    const owner = notes.find((n) => n.attachments.some((a) => a.id === attId));
    if (!owner) return json({ detail: "unknown attachment" }, 404);
    owner.attachments = owner.attachments.filter((a) => a.id !== attId);
    owner.ingest_state = "pending";
    attachmentBlobs.delete(attId);
    attachmentExtracts.delete(attId);
    return new Response(null, { status: 204 });
  }
  if (blobMatch) {
    const blob = attachmentBlobs.get(decodeURIComponent(blobMatch[1] ?? ""));
    if (!blob) return json({ detail: "unknown attachment" }, 404);
    return new Response(blob, { status: 200, headers: { "Content-Type": blob.type } });
  }

  // Image launcher (Wave L1). The gallery list, and the direct generate/edit
  // renders that fabricate a row after a short await (the honest render delay)
  // and prepend it newest-first. The edit reads the source's dims via the spec's
  // sourceImageId. Mock-only until the L3 render API.
  if (path === "/api/images/generated" && method === "GET") {
    return json(imageGallery);
  }
  if (path === "/api/images/generate" && method === "POST") {
    const spec = JSON.parse(String(init?.body)) as MockGenSpec;
    return json(recordRender("generate", spec));
  }
  if (path === "/api/images/edit" && method === "POST") {
    // The real call is multipart (spec part + source/reference files); here we
    // only need the spec part to fabricate the row.
    let spec: MockGenSpec = {};
    const body = init?.body;
    if (body instanceof FormData) {
      const raw = body.get("spec");
      if (typeof raw === "string") spec = JSON.parse(raw) as MockGenSpec;
    }
    return json(recordRender("edit", spec));
  }

  // Generated-image bytes (Wave G3): the image-gen tool's result, by id, plus an
  // edit's `/source` ("before"). The real backend serves owner-only bytes; here
  // any seeded id round-trips a placeholder PNG (the in-chat <img> uses a data:
  // URI helper, since an <img src> never flows through mockFetch). 404 unknown.
  const genImgMatch = path.match(/^\/api\/images\/generated\/([^/]+)(\/source)?$/);
  if (genImgMatch && method === "GET") {
    const imgId = decodeURIComponent(genImgMatch[1] ?? "");
    if (!GENERATED_IMAGES.has(imgId)) return json({ detail: "image not found" }, 404);
    return new Response(TRANSPARENT_PNG, {
      status: 200,
      headers: { "Content-Type": "image/png" },
    });
  }
  // Row-only delete: drop it from the in-memory gallery; the blob (here a shared
  // placeholder) is keep-all, mirroring the backend. 404 an unknown id.
  if (genImgMatch && !genImgMatch[2] && method === "DELETE") {
    const imgId = decodeURIComponent(genImgMatch[1] ?? "");
    if (!GENERATED_IMAGES.has(imgId)) return json({ detail: "image not found" }, 404);
    imageGallery = imageGallery.filter((g) => g.id !== imgId);
    GENERATED_IMAGES.delete(imgId);
    return new Response(null, { status: 204 });
  }

  {
    const noteMatch = path.match(/^\/api\/notes\/([^/]+)$/);
    if (noteMatch && (!init?.method || init.method === "GET")) {
      const note = notes.find((n) => n.id === decodeURIComponent(noteMatch[1] ?? ""));
      return note ? json(note) : json({ detail: "note not found" }, 404);
    }
  }
  const analysisMatch = path.match(/^\/api\/notes\/([^/]+)\/analysis$/);
  if (analysisMatch && method === "GET") {
    const noteId = decodeURIComponent(analysisMatch[1] ?? "");
    const note = notes.find((n) => n.id === noteId);
    if (!note) return json({ detail: "note not found" }, 404);
    // Notes without an analysis fixture read as not-yet-analyzed.
    return json(ANALYSES[noteId] ?? emptyAnalysis(noteId));
  }

  if (path === "/api/entities" && method === "GET") {
    return json({ items: mockEntityList(url.searchParams) });
  }

  if (path === "/api/graph" && method === "GET") {
    return json(mockFullGraph());
  }

  const neighborsMatch = path.match(/^\/api\/entities\/([^/]+)\/neighbors$/);
  if (neighborsMatch && method === "GET") {
    const graph = mockNeighbors(
      decodeURIComponent(neighborsMatch[1] ?? ""),
      Number(url.searchParams.get("depth") ?? "2"),
    );
    return graph ? json(graph) : json({ detail: "entity not found" }, 404);
  }

  const entityImageMatch = path.match(/^\/api\/entities\/([^/]+)\/image$/);
  if (entityImageMatch) {
    const entity = ENTITIES[decodeURIComponent(entityImageMatch[1] ?? "")];
    if (!entity) return json({ detail: "entity not found" }, 404);
    if (method === "PUT") {
      // Mint a sha so the refetched entity re-renders the image slot (the bytes are ignored).
      entity.image_sha = id("img");
      return json({ image_sha: entity.image_sha, media_type: "image/png" });
    }
    if (method === "GET") {
      if (!entity.image_sha) return json({ detail: "no image" }, 404);
      return new Response(TRANSPARENT_PNG, {
        status: 200,
        headers: { "Content-Type": "image/png" },
      });
    }
  }

  const entityMatch = path.match(/^\/api\/entities\/([^/]+)$/);
  if (entityMatch && method === "GET") {
    const entity = ENTITIES[decodeURIComponent(entityMatch[1] ?? "")];
    return entity ? json(entity) : json({ detail: "entity not found" }, 404);
  }

  if (path === "/api/wiki/landing" && method === "GET") {
    return json(WIKI_LANDING);
  }

  const correctionMatch = path.match(/^\/api\/wiki\/([^/]+)\/corrections$/);
  if (correctionMatch && method === "POST") {
    return json({ note_id: id("note"), created: true }, 201);
  }

  // Talk board — most-specific routes first (editor, reply, status, new-topic, board).
  const talkEditorMatch = path.match(/^\/api\/wiki\/([^/]+)\/talk\/topics\/([^/]+)\/editor$/);
  if (talkEditorMatch && method === "POST") {
    const board = WIKI_TALK[decodeURIComponent(talkEditorMatch[1] ?? "")];
    const topic = board?.topics.find((t) => t.id === decodeURIComponent(talkEditorMatch[2] ?? ""));
    if (!topic) return json({ detail: "topic not found" }, 404);
    if (topic.kind === "build_log")
      return json({ detail: "the Build log is machine-written" }, 409);
    // A canned Editor turn: explains the sourcing and files a correction (with an outcome chip).
    const post = {
      id: id("post"),
      author: "editor" as const,
      body: "That claim cites one note with no later departure. I filed your correction; the article will rebuild from the corrected graph.",
      source: null,
      outcome: "correction filed → rebuild queued",
      created_at: new Date().toISOString(),
      rev: null,
    };
    topic.posts.push(post);
    return json({ post });
  }

  const talkReplyMatch = path.match(/^\/api\/wiki\/([^/]+)\/talk\/topics\/([^/]+)\/posts$/);
  if (talkReplyMatch && method === "POST") {
    const board = WIKI_TALK[decodeURIComponent(talkReplyMatch[1] ?? "")];
    const topic = board?.topics.find((t) => t.id === decodeURIComponent(talkReplyMatch[2] ?? ""));
    if (!topic) return json({ detail: "topic not found" }, 404);
    if (topic.kind === "build_log")
      return json({ detail: "the Build log is machine-written" }, 409);
    const body = (JSON.parse(String(init?.body)) as { body: string }).body;
    const post = {
      id: id("post"),
      author: "owner" as const,
      body,
      source: null,
      outcome: null,
      created_at: new Date().toISOString(),
      rev: null,
    };
    topic.posts.push(post);
    return json(post, 201);
  }

  const talkStatusMatch = path.match(/^\/api\/wiki\/([^/]+)\/talk\/topics\/([^/]+)$/);
  if (talkStatusMatch && method === "PATCH") {
    const board = WIKI_TALK[decodeURIComponent(talkStatusMatch[1] ?? "")];
    const topic = board?.topics.find((t) => t.id === decodeURIComponent(talkStatusMatch[2] ?? ""));
    if (!topic) return json({ detail: "topic not found" }, 404);
    if (topic.kind === "build_log")
      return json({ detail: "the Build log is machine-written" }, 409);
    const status = (JSON.parse(String(init?.body)) as { status: "open" | "resolved" }).status;
    topic.status = status;
    return json({ id: topic.id, status });
  }

  const talkTopicsMatch = path.match(/^\/api\/wiki\/([^/]+)\/talk\/topics$/);
  if (talkTopicsMatch && method === "POST") {
    const board = WIKI_TALK[decodeURIComponent(talkTopicsMatch[1] ?? "")];
    if (!board) return json({ detail: "article not found" }, 404);
    const payload = JSON.parse(String(init?.body)) as { title: string; body: string };
    const topic: WikiTalkTopic = {
      id: id("topic"),
      kind: "discussion",
      title: payload.title,
      status: "open",
      meta: null,
      posts: [
        {
          id: id("post"),
          author: "owner",
          body: payload.body,
          source: null,
          outcome: null,
          created_at: new Date().toISOString(),
          rev: null,
        },
      ],
    };
    board.topics.unshift(topic);
    return json(topic, 201);
  }

  const talkMatch = path.match(/^\/api\/wiki\/([^/]+)\/talk$/);
  if (talkMatch && method === "GET") {
    const board = WIKI_TALK[decodeURIComponent(talkMatch[1] ?? "")];
    return board ? json(board) : json({ detail: "article not found" }, 404);
  }

  const wikiMatch = path.match(/^\/api\/wiki\/([^/]+)$/);
  if (wikiMatch && method === "GET") {
    const article = WIKI_ARTICLES[decodeURIComponent(wikiMatch[1] ?? "")];
    return article ? json(article) : json({ detail: "article not found" }, 404);
  }

  if (path === "/api/review" && method === "GET") {
    const status = url.searchParams.get("status") ?? "open";
    // Mirrors the backend: the decided log folds in dismissals and reopened
    // tombstones (still open, marker set) but never the deferred lane, which
    // is its own list; both decided/deferred sort newest decision first.
    const items =
      status === "open"
        ? REVIEW_ITEMS.filter((item) => item.status === "open")
        : status === "deferred"
          ? REVIEW_ITEMS.filter((item) => item.status === "deferred").sort((a, b) =>
              decidedAt(b).localeCompare(decidedAt(a)),
            )
          : REVIEW_ITEMS.filter(
              (item) =>
                item.status === "resolved" ||
                item.status === "dismissed" ||
                (item.status === "open" && item.resolution?.reopened_at !== undefined),
            ).sort((a, b) => decidedAt(b).localeCompare(decidedAt(a)));
    return json({ items });
  }

  if (path === "/api/review/resolve-batch" && method === "POST") {
    const body = JSON.parse(String(init?.body)) as {
      decisions: { id: string; action: string; payload?: Record<string, unknown> }[];
    };
    const items: ReviewItem[] = [];
    const errors: { id: string; detail: string }[] = [];
    for (const d of body.decisions) {
      const item = REVIEW_ITEMS.find((r) => r.id === d.id);
      if (!item) errors.push({ id: d.id, detail: "not found" });
      else if (item.status !== "open") errors.push({ id: d.id, detail: "not open" });
      else if (!advertisedActions(item).has(d.action))
        errors.push({ id: d.id, detail: `invalid action ${d.action}` });
      else items.push({ ...applyResolution(item, d.action, d.payload ?? {}) });
    }
    return json({ items, errors });
  }

  const resolveMatch = path.match(/^\/api\/review\/([^/]+)\/resolve$/);
  if (resolveMatch && method === "POST") {
    const item = REVIEW_ITEMS.find((r) => r.id === decodeURIComponent(resolveMatch[1] ?? ""));
    if (!item) return json({ detail: "review item not found" }, 404);
    if (item.status !== "open") return json({ detail: "review item is not open" }, 409);
    const body = JSON.parse(String(init?.body)) as {
      action: string;
      payload?: Record<string, unknown>;
    };
    // Mirror the backend's contract: only advertised actions resolve;
    // anything else is a 400, the item untouched.
    if (!advertisedActions(item).has(body.action)) {
      return json({ detail: `action ${body.action} is not valid for kind ${item.kind}` }, 400);
    }
    return json(applyResolution(item, body.action, body.payload ?? {}));
  }

  const reopenMatch = path.match(/^\/api\/review\/([^/]+)\/reopen$/);
  if (reopenMatch && method === "POST") {
    const item = REVIEW_ITEMS.find((r) => r.id === decodeURIComponent(reopenMatch[1] ?? ""));
    if (!item) return json({ detail: "review item not found" }, 404);
    if (item.status === "open") return json({ detail: "review item is already open" }, 409);
    // Un-parking a deferred item is a clean re-queue: no tombstone, no note.
    if (item.status === "deferred") {
      item.status = "open";
      item.resolved_at = null;
      item.resolution = null;
      return json({ ...item, reopen_note: null });
    }
    const keptEdge = (item.resolution?.effects ?? []).some((e) => e.action === "distinct_from");
    item.status = "open";
    item.resolved_at = null;
    item.resolution = {
      ...(item.resolution ?? { action: "dismiss", payload: {} }),
      reopened_at: new Date().toISOString(),
    };
    return json({
      ...item,
      reopen_note: keptEdge
        ? "the distinct-from edge is permanent and stays — this pair is never re-proposed"
        : null,
    });
  }

  if (path.startsWith("/api/ops/metrics/history")) {
    // A synthetic 120-bucket series with gentle sine waves so the graphs render.
    const now = Date.now();
    const points = Array.from({ length: 120 }, (_, i) => {
      const wave = Math.sin(i / 9);
      return {
        t: new Date(now - (119 - i) * 60_000).toISOString(),
        load_1m: Math.round((1.2 + wave) * 100) / 100,
        load_5m: 1.1,
        load_15m: 1.0,
        mem_used_bytes: Math.round((60 + wave * 6) * 2 ** 30),
        mem_total_bytes: 128 * 2 ** 30,
        swap_used_bytes: 0,
        disk_used_bytes: 500 * 2 ** 30,
        disk_total_bytes: 2000 * 2 ** 30,
        gpu_busy_percent: Math.max(0, Math.round(40 + wave * 30)),
        fan_rpm_max: Math.round(2000 + wave * 400),
        power_w: Math.round((25 + wave * 15) * 10) / 10,
      };
    });
    return json({
      resolution: "raw",
      step_seconds: 60,
      since: points[0]?.t,
      until: points[points.length - 1]?.t,
      points,
    });
  }

  if (path === "/api/ops/llm-usage") return json(LLM_USAGE);

  if (path === "/api/ops/update" && init?.method === "POST") {
    mockUpdate.state = "running";
    mockUpdate.ticks = 0;
    return json({ updater: "jbrain-updater-mock" }, 202);
  }
  if (path === "/api/ops/update/status") {
    if (mockUpdate.state === "running" && ++mockUpdate.ticks >= 3) {
      mockUpdate.state = "exited";
    }
    return json({
      state: mockUpdate.state,
      exit_code: mockUpdate.state === "exited" ? 0 : null,
      log_tail:
        mockUpdate.state === "none"
          ? ""
          : `[update] starting\n[update] building images\n${
              mockUpdate.state === "exited" ? "[update] complete" : ""
            }`,
    });
  }
  if (path === "/api/ops/export" && init?.method === "POST") {
    mockExport.state = "running";
    mockExport.ticks = 0;
    return json({ oneshot: "jbrain-export-mock" }, 202);
  }
  if (path === "/api/ops/export/status") {
    if (mockExport.state === "running" && ++mockExport.ticks >= 3) {
      mockExport.state = "exited";
    }
    const done = mockExport.state === "exited";
    return json({
      state: mockExport.state,
      exit_code: done ? 0 : null,
      log_tail: mockExport.state === "none" ? "" : "[export] dumping database",
      filename: done ? "export-20260610-133800.jbrain.tar" : null,
    });
  }
  if (path.startsWith("/api/ops/export/file/")) {
    return new Response(new Blob(["mock export archive"]), { status: 200 });
  }
  if (path === "/api/ops/import/upload" && init?.method === "POST") {
    return json({ archive: "import-20260610-134500.jbrain.tar" }, 201);
  }
  if (path === "/api/ops/import/start" && init?.method === "POST") {
    mockImport.state = "running";
    mockImport.ticks = 0;
    return json({ oneshot: "jbrain-import-mock" }, 202);
  }
  if (path === "/api/ops/reset" && init?.method === "POST") {
    // Mirror reset-inner.sh: content data goes, auth/domains/usage stay.
    // Zeroing the fixtures here lets dev:mock round-trip the whole flow.
    notes.length = 0;
    attachmentBlobs.clear();
    attachmentExtracts.clear();
    analyzingAttachments.clear();
    analyzingNotes.clear();
    REVIEW_ITEMS.length = 0;
    for (const key of Object.keys(ANALYSES)) delete ANALYSES[key];
    for (const key of Object.keys(ENTITIES)) delete ENTITIES[key];
    mockReset.state = "running";
    mockReset.ticks = 0;
    return json({ oneshot: "jbrain-reset-mock" }, 202);
  }
  if (path === "/api/ops/reset/status") {
    if (mockReset.state === "running" && ++mockReset.ticks >= 3) {
      mockReset.state = "exited";
    }
    return json({
      state: mockReset.state,
      exit_code: mockReset.state === "exited" ? 0 : null,
      log_tail:
        mockReset.state === "none"
          ? ""
          : `[reset] safety backup\n[reset] truncating content tables\n${
              mockReset.state === "exited" ? "[reset] complete" : ""
            }`,
    });
  }
  if (path === "/api/ops/import/status") {
    if (mockImport.state === "running" && ++mockImport.ticks >= 4) {
      mockImport.state = "exited";
    }
    return json({
      state: mockImport.state,
      exit_code: mockImport.state === "exited" ? 0 : null,
      log_tail:
        mockImport.state === "none"
          ? ""
          : `[import] safety backup of current data\n[import] restoring database\n${
              mockImport.state === "exited" ? "[import] complete" : ""
            }`,
    });
  }
  if (path === "/api/ops/metrics") {
    return json({
      mem_total_bytes: 4 * 2 ** 30,
      mem_available_bytes: 1.2 * 2 ** 30,
      swap_total_bytes: 2 * 2 ** 30,
      swap_free_bytes: 1.9 * 2 ** 30,
      disk_total_bytes: 40 * 2 ** 30,
      disk_free_bytes: 24 * 2 ** 30,
      load_1m: 0.42,
      load_5m: 0.31,
      load_15m: 0.2,
      uptime_seconds: 3 * 86400 + 7 * 3600,
      gpu_busy_percent: 63,
      apu_power_w: 28.5,
      fan_rpm: { "CPU fan": 2100, "System fan": 1850 },
      containers: CONTAINERS.map((c, i) => ({
        service: c.service,
        mem_bytes: (i + 1) * 90 * 2 ** 20,
      })),
      processes: [
        {
          service: "local-llm",
          pid: 101,
          rss_bytes: 1.9 * 2 ** 30,
          command: "llama-server --model /models/gpt-oss-120b/x.gguf",
        },
        {
          service: "local-llm",
          pid: 102,
          rss_bytes: 1.1 * 2 ** 30,
          command: "llama-server --model /models/qwen3-vl-30b/x.gguf",
        },
        {
          service: "comfyui",
          pid: 201,
          rss_bytes: 320 * 2 ** 20,
          command: "python /opt/ComfyUI/main.py",
        },
        { service: "embed", pid: 301, rss_bytes: 180 * 2 ** 20, command: "text-embeddings-router" },
        { service: "api", pid: 401, rss_bytes: 140 * 2 ** 20, command: "uvicorn jbrain.main:app" },
        { service: "db", pid: 501, rss_bytes: 96 * 2 ** 20, command: "postgres" },
      ],
      db: {
        db_size_bytes: 38 * 2 ** 20,
        note_count: 47,
        attachment_count: 9,
        attachment_bytes: 21 * 2 ** 20,
      },
      blobs: { file_count: 9, total_bytes: 21 * 2 ** 20 },
    });
  }
  if (path === "/api/ops/status") return json({ containers: CONTAINERS });
  if (path === "/api/ops/restart") return new Response(null, { status: 204 });
  if ((path === "/api/ops/start" || path === "/api/ops/stop") && init?.method === "POST") {
    return json({ service: "mock", action: path.endsWith("start") ? "start" : "stop" }, 202);
  }
  if (path === "/api/ops/rebuild" && init?.method === "POST") {
    mockRebuild.state = "running";
    mockRebuild.ticks = 0;
    return json({ oneshot: "jbrain-rebuild-mock" }, 202);
  }
  if (path === "/api/ops/rebuild/status") {
    if (mockRebuild.state === "running" && ++mockRebuild.ticks >= 2) {
      mockRebuild.state = "exited";
    }
    return json({
      state: mockRebuild.state,
      exit_code: mockRebuild.state === "exited" ? 0 : null,
      log_tail: mockRebuild.state === "none" ? "" : "[rebuild] wall: building image",
    });
  }

  // The Runs surface (owner-only run log) + the sweep-trigger controls. Filtering is
  // server-side (mirrors backend/src/jbrain/agent/runlog.py) so the mock exercises the
  // same kind / hide-sweeps / date-range / limit path the real API drives.
  const runIsSweep = (r: RunSummary): boolean =>
    r.kind === "pipeline" &&
    (r.name.startsWith("reconcile_") ||
      r.name === "geofence_sweep" ||
      r.name === "purge_deleted_artifacts");
  const runBucket = (kind: string): string =>
    kind === "agent" || kind === "subagent"
      ? "agent"
      : kind === "integration"
        ? "integration"
        : "pipeline";
  if (path === "/api/runs/queue-depth") {
    return json({ queued: MOCK_RUNS.filter((r) => r.status === "queued").length });
  }
  if (path === "/api/runs/stats") {
    const sp = url.searchParams;
    const excludeSweeps = sp.get("exclude_sweeps") === "true";
    const sinceStr = sp.get("since");
    const since = sinceStr ? Date.parse(sinceStr) : null;
    const dayStart = new Date();
    dayStart.setUTCHours(0, 0, 0, 0);
    const day = dayStart.getTime();
    const today = MOCK_RUNS.filter((r) => Date.parse(r.started_at) >= day);
    const byKind: Record<string, number> = { agent: 0, integration: 0, pipeline: 0 };
    for (const r of MOCK_RUNS) {
      if (since !== null && Date.parse(r.started_at) < since) continue;
      if (excludeSweeps && runIsSweep(r)) continue;
      const b = runBucket(r.kind);
      byKind[b] = (byKind[b] ?? 0) + 1;
    }
    return json({
      active: MOCK_RUNS.filter((r) => r.status === "running").length,
      failed_today: today.filter((r) => r.status === "error").length,
      tokens_today: today.reduce((sum, r) => sum + r.cost_tokens, 0),
      by_kind: byKind,
    });
  }
  if (path === "/api/runs") {
    const sp = url.searchParams;
    const kinds = sp.getAll("kinds");
    const excludeSweeps = sp.get("exclude_sweeps") === "true";
    const sinceStr = sp.get("since");
    const since = sinceStr ? Date.parse(sinceStr) : null;
    const limit = Number(sp.get("limit") ?? "50");
    const rows = MOCK_RUNS.filter((r) => {
      if (kinds.length && !kinds.includes(r.kind)) return false;
      if (excludeSweeps && runIsSweep(r)) return false;
      if (since !== null && Date.parse(r.started_at) < since) return false;
      return true;
    }).slice(0, Math.max(1, Math.min(limit, 200)));
    return json(rows);
  }
  const runMatch = path.match(/^\/api\/runs\/([^/]+)$/);
  if (runMatch) {
    const detail = MOCK_RUN_DETAILS.find((r) => r.id === decodeURIComponent(runMatch[1] ?? ""));
    return detail ? json(detail) : json({ detail: "no run with that id in scope" }, 404);
  }
  if (path === "/api/ops/triggers") return json(MOCK_SWEEPS);
  if (/^\/api\/ops\/triggers\/[^/]+\/run$/.test(path) && method === "POST") {
    return new Response(null, { status: 202 });
  }
  if (path.startsWith("/api/ops/logs/")) {
    const lines = Array.from(
      { length: 40 },
      (_, i) => `${new Date().toISOString()} mock log line ${i + 1}`,
    );
    return new Response(lines.join("\n"), { status: 200 });
  }

  // Phase 7 location: the owner's Devices tab read + key management.
  if (path === "/api/locations/devices" && method === "GET") {
    return json(MOCK_DEVICES.map(deviceSummary));
  }
  if (path === "/api/locations/timeline" && method === "GET") return json(MOCK_TIMELINE);
  if (path === "/api/locations/places" && method === "GET") return json(MOCK_PLACES);
  if (path === "/api/locations/fixes" && method === "GET") return json(mockTrail());
  // The member dashboard (/dash) reads: a roster with each visible subject's latest
  // coordinate (for the map pins) + the shared fences.
  if (path === "/api/member/roster" && method === "GET") {
    const now = Date.now();
    return json([
      {
        subject_id: "subj-me",
        label: "You",
        last_seen: new Date(now - 90_000).toISOString(),
        battery_pct: 64,
        connection: "wifi",
        latitude: 40.014,
        longitude: -74.205,
        velocity_mps: null,
        is_self: true,
      },
      {
        subject_id: "subj-sam",
        label: "Sam",
        last_seen: new Date(now - 7 * 60_000).toISOString(),
        battery_pct: 88,
        connection: "cell",
        latitude: 40.02,
        longitude: -74.19,
        velocity_mps: 13.4, // ≈ 30 mph — traveling
        is_self: false,
      },
      {
        subject_id: "subj-ro",
        label: "Ro",
        last_seen: new Date(now - 40 * 60_000).toISOString(),
        battery_pct: 21,
        connection: null,
        latitude: 40.008,
        longitude: -74.225,
        velocity_mps: null,
        is_self: false,
      },
    ]);
  }
  if (path === "/api/member/places" && method === "GET") return json(MOCK_PLACES);
  if (path.startsWith("/api/member/positions") && method === "GET") return json(mockTrail());
  if (path === "/api/member/timeline" && method === "GET") {
    const now = Date.now();
    const at = (minsAgo: number) => new Date(now - minsAgo * 60_000).toISOString();
    return json([
      {
        occurred_at: at(8),
        subject_id: "subj-me",
        transition: "enter",
        place_entity_id: "ent-home",
        place_name: "Home",
      },
      {
        occurred_at: at(95),
        subject_id: "subj-me",
        transition: "exit",
        place_entity_id: "ent-work",
        place_name: "Work",
      },
      {
        occurred_at: at(540),
        subject_id: "subj-me",
        transition: "enter",
        place_entity_id: "ent-work",
        place_name: "Work",
      },
      {
        occurred_at: at(40),
        subject_id: "subj-sam",
        transition: "enter",
        place_entity_id: "ent-gym",
        place_name: "Gym",
      },
    ]);
  }
  if (path === "/api/locations/geocode" && method === "GET") {
    return json({ address: "12 Market St, Springfield" });
  }
  if (path === "/api/pairing/codes" && method === "POST") {
    const body = init?.body ? (JSON.parse(String(init.body)) as { device_id?: string }) : {};
    // Re-pair restores a revoked phone (redeeming the code rotates its key).
    if (body.device_id) {
      const target = MOCK_DEVICES.find((d) => d.id === body.device_id);
      if (target) target.revoked = false;
    }
    const code = `mock-code-${mockDeviceSeq++}`;
    // The payload mirrors the backend: base64url(JSON {v,u,c}) embedding the server.
    const payload = btoa(JSON.stringify({ v: 1, u: window.location.origin, c: code }))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
    return json({ code, expires_at: new Date(Date.now() + 3_600_000).toISOString(), payload }, 201);
  }
  const rename = path.match(/^\/api\/devices\/([^/]+)\/rename$/);
  if (rename && method === "POST") {
    const target = MOCK_DEVICES.find((d) => d.id === decodeURIComponent(rename[1] ?? ""));
    const label = init?.body
      ? (JSON.parse(String(init.body)) as { label?: string }).label
      : undefined;
    if (target && label) target.label = label;
    return new Response(null, { status: 204 });
  }
  const revoke = path.match(/^\/api\/devices\/([^/]+)\/revoke$/);
  if (revoke && method === "POST") {
    const target = MOCK_DEVICES.find((d) => d.id === decodeURIComponent(revoke[1] ?? ""));
    if (target) target.revoked = true;
    return new Response(null, { status: 204 });
  }
  const del = path.match(/^\/api\/devices\/([^/]+)$/);
  if (del && method === "DELETE") {
    const idx = MOCK_DEVICES.findIndex((d) => d.id === decodeURIComponent(del[1] ?? ""));
    if (idx >= 0) MOCK_DEVICES.splice(idx, 1);
    return new Response(null, { status: 204 });
  }

  return json({ detail: `mock: no route for ${method} ${path}` }, 404);
};
