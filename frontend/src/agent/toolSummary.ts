// Turn a raw tool activity into a tidy "step" for the collapsed Worked block
// under a response. Pure so it's unit-testable. Structured `sources` from the
// result event are preferred; we fall back to parsing the known search/read_note
// summary text (backend readtools) so older streams still render. Every tool in
// the backend roster gets a friendly label AND an inline-arg policy here —
// enforced by backend/tests/unit/test_tool_step_polish.py, so a new `.tool`
// cannot ship as a raw snake_case row with no visible target.

import type { SourceRef, ToolActivity } from "./transcript";
import type { EntityRef, FactWrite, WebSource } from "./types";

export type { SourceRef };

export interface ToolStep {
  id: string;
  name: string;
  ok: boolean | undefined;
  label: string;
  /** The call's one human-readable target — the searched query, fetched url,
   * chosen action — shown inline to the right of the label on the step row. */
  inline: string | undefined;
  sources: SourceRef[];
  /** Web pages a jerv internet tool reached — favicon link cards in the expanded
   * step, and the targets a `[^n]` web citation resolves to. */
  webSources: WebSource[];
  /** Entities the tool resolved — rendered as tappable links in the expanded
   * step, so a name reaches its page without exposing the raw id. */
  entities: EntityRef[];
  /** Graph writes this call made — the D3 "entity modified" rung of the expanded
   * step. Empty for every tool that writes nothing. */
  facts: FactWrite[];
  /** The call asserted only a prefix of what it was given, so its write list is
   * partial and says so (D3's `truncated`). */
  truncated: boolean;
  /** The call's arguments, for the expanded-step "arguments" list. */
  args: Record<string, unknown> | undefined;
  /** The verbatim result text, for the expanded step's result/raw rung. */
  summary: string | undefined;
}

const STEP_LABELS: Record<string, string> = {
  // Knowledge base
  search: "Searched your notes",
  read_note: "Read a note",
  read_entity: "Read an entity",
  find_entity: "Found an entity",
  relate: "Followed a relationship",
  neighborhood: "Mapped an entity's neighborhood",
  read_wiki: "Read a wiki article",
  request_rebuild: "Requested an article rebuild",
  add_source_exclusion: "Excluded a source",
  file_correction: "Filed a correction note",
  // The note conversation's write surface (AGENT_INGEST_CONVERSATION_PLAN D3/D16).
  // Labelled in the owner's terms, never the verb's: the step says what the agent did
  // to the owner's graph, and the expanded rung says what landed and in which domain.
  resolve_entity: "Resolved who the note means",
  assert_fact: "Recorded what the note says",
  // `close_reading` is the whole-note reading (AGENT_INGEST_REWRITE R1), not one more
  // fact — the label says "read", because what it recorded is everything the note says
  // and the difference is what the settle acts on.
  close_reading: "Read the whole note",
  ask_owner: "Asked you a question",
  prefs_read: "Read your standing instructions",
  prefs_write: "Staged a standing instruction",
  // The on-reply half (D8). `correct_fact` WROTE — it replaced a value and pinned it —
  // while `merge_entities` only staged, and the two labels have to say which, because
  // "staged" is the whole difference between something you still have to approve and
  // something already on file.
  correct_fact: "Corrected a fact you disputed",
  merge_entities: "Staged an entity fold",
  propose_correction: "Staged a proposal",
  propose_merge: "Staged an entity merge",
  // Memory + scratchpads
  recall: "Recalled past notes",
  memory_read: "Read memory",
  memory_edit: "Updated its scratchpad",
  remember: "Staged a memory change",
  archivist_memory_read: "Read memory",
  archivist_memory_write: "Updated memory",
  scratch_list: "Listed its scratchpad",
  scratch_read: "Read its scratchpad",
  scratch_write: "Wrote its scratchpad",
  scratch_manage: "Tidied its scratchpad",
  journal: "Wrote a journal entry",
  name_session: "Named the session",
  time_left: "Checked time remaining",
  // Web + research
  web_search: "Searched the web",
  web_fetch: "Read a web page",
  news_search: "Searched the news",
  news_feed: "Read a news feed",
  science_search: "Searched scientific papers",
  public_records: "Searched public records",
  portal_search: "Searched public portals",
  grokipedia: "Consulted Grokipedia",
  check_channel: "Checked a channel",
  deep_research: "Ran deep research",
  deepest_research: "Ran deepest research",
  deep_produce: "Produced a research deliverable",
  decompose_research: "Decomposed the research",
  research_report: "Checked research reports",
  show_research_report: "Showed a research report",
  remove_research_report: "Removed a research report",
  spawn_subagent: "Spawned sub-agents",
  queued: "Queued a job",
  // Gmail
  gmail_search: "Searched Gmail",
  gmail_read: "Read an email",
  gmail_count: "Counted emails",
  gmail_archive: "Archived an email",
  gmail_label: "Relabeled an email",
  gmail_bulk_label: "Relabeled emails",
  gmail_create_label: "Created a label",
  gmail_list_labels: "Listed labels",
  gmail_sender_breakdown: "Broke down senders",
  // Health
  read_labs: "Read lab results",
  read_encounters: "Read medical encounters",
  chart_measurements: "Charted measurements",
  make_intake_link: "Made an intake link",
  // Lists + appointments + plans
  create_list: "Created a list",
  read_list: "Read a list",
  read_lists: "Read your lists",
  add_list_item: "Added a list item",
  remove_list_item: "Removed a list item",
  check_list_item: "Ticked a list item",
  manage_appointment: "Managed an appointment",
  read_appointment: "Read an appointment",
  read_appointments: "Read your appointments",
  read_plan: "Read the plan",
  write_plan: "Wrote the plan",
  write_plan_result: "Logged a plan step",
  read_artifact: "Read an artifact",
  // Location + time + home
  current_location: "Checked your location",
  current_time: "Checked the clock",
  location_history: "Read location history",
  location_query: "Checked a place",
  find_when_at: "Checked when you were somewhere",
  time_at_place: "Tallied time at a place",
  where_is: "Checked where someone is",
  where_was_i: "Retraced where you were",
  nearby_now: "Checked what's nearby",
  save_place: "Saved a place",
  geocode_reverse: "Looked up coordinates",
  home_status: "Checked home status",
  device_status: "Checked device status",
  query_server_metrics: "Checked server metrics",
  // Weather
  weather: "Checked the weather",
  weather_history: "Checked past weather",
  hurricane: "Checked the hurricane outlook",
  // Media + vision
  analyze_image: "Analyzed an image",
  analyze_video: "Analyzed a video",
  analyze_stream: "Analyzed a stream",
  compare_images: "Compared images",
  crop_regions: "Cropped image regions",
  fetch_image: "Fetched an image",
  grab_frame: "Grabbed a video frame",
  ocr: "Read text off an image",
  transcribe: "Transcribed audio",
  sdr_listen: "Tuned the radio",
  sdr_stop: "Released the radio",
  aprs_recent: "Read the APRS log",
  sdr_aprs_logging: "Set APRS logging",
  sdr_read: "Read the band plan",
  sdr_signal: "Measured a signal",
  external_video: "Checked saved videos",
  show_external_video: "Showed a video",
  remove_external_video: "Removed a saved video",
  // Charts + canvases
  render_chart: "Rendered a chart",
  render_bars: "Rendered a bar chart",
  render_html: "Rendered a figure",
  canvas: "Drew on a canvas",
  show_canvas: "Showed a canvas",
  // Moltbook (jmolt) + its observer
  moltbook: "Browsed Moltbook",
  moltbook_post: "Posted to Moltbook",
  moltbook_comment: "Commented on Moltbook",
  moltbook_vote: "Voted on Moltbook",
  moltbook_social: "Managed Moltbook follows",
  moltbook_profile_update: "Updated its Moltbook profile",
  jmolt_observe: "Observed jmolt",
};

function stepLabel(name: string): string {
  if (STEP_LABELS[name]) return STEP_LABELS[name];
  if (name.startsWith("lookup_")) return `Checked ${name.slice(7).replace(/_/g, " ")}`;
  return name;
}

// The argument(s) worth showing on a tool's collapsed row: the "what" a generic
// label ("Searched Gmail", "Observed jmolt") leaves implicit — the query it ran,
// the url it fetched, the action it took. Ordered candidate keys per tool; the
// first INLINE_PIECES present render joined with " · " (so an umbrella tool reads
// "scratch_read · index.md"). Opaque ids (message_id, note_id, entity_id…) stay
// out of these lists — a tool whose every argument is opaque opts out via
// NO_INLINE instead, so the roster gate can tell "considered" from "forgot".
const INLINE_ARGS: Record<string, readonly string[]> = {
  search: ["query"],
  recall: ["query"],
  web_search: ["query"],
  web_fetch: ["url"],
  news_search: ["query"],
  news_feed: ["category"],
  sdr_listen: ["frequency_mhz"],
  aprs_recent: ["source"],
  sdr_aprs_logging: ["frequency_mhz"],
  sdr_read: ["section", "what"],
  sdr_signal: ["frequency_mhz", "section"],
  science_search: ["query"],
  public_records: ["name", "state"],
  portal_search: ["name", "jurisdiction"],
  grokipedia: ["action", "query", "slug"],
  check_channel: ["channel_id"],
  deep_research: ["question", "preset"],
  deepest_research: ["question"],
  deep_produce: ["output_kind", "question"],
  research_report: ["action", "query", "question"],
  show_research_report: ["question"],
  remove_research_report: ["question"],
  gmail_search: ["query"],
  gmail_count: ["query"],
  gmail_bulk_label: ["query"],
  gmail_sender_breakdown: ["query"],
  gmail_create_label: ["name"],
  find_entity: ["name"],
  neighborhood: ["anchor"],
  relate: ["relationship"],
  lookup_medication: ["name"],
  lookup_condition: ["name"],
  add_source_exclusion: ["domain", "reason"],
  file_correction: ["body"],
  // The W3 write tools batch (TOOL_SURFACE.md: ≤12 surfaces, ≤8 facts per call), so
  // their one legible target is an ARRAY — `inlinePiece` renders those elementwise.
  resolve_entity: ["entities"],
  assert_fact: ["facts"],
  // The reading's legible target is its TITLE — the one line the owner would recognise
  // the note by — with the facts behind it, elementwise, as the second piece.
  close_reading: ["title", "facts"],
  ask_owner: ["question"],
  // The on-reply writes are NOT batched — one disputed value, one pair of entities — so
  // their inline piece is the thing the owner would recognise in the strip: what the
  // fact was corrected TO, and which two records are being folded.
  correct_fact: ["statement"],
  merge_entities: ["entity_a", "entity_b"],
  propose_correction: ["correction"],
  propose_merge: ["reason"],
  remember: ["body_md"],
  memory_read: ["block_kind"],
  memory_edit: ["op"],
  archivist_memory_write: ["content"],
  prefs_write: ["op", "text"],
  scratch_read: ["filename"],
  scratch_write: ["mode", "filename"],
  scratch_manage: ["op", "filename"],
  journal: ["entry"],
  name_session: ["name"],
  read_labs: ["analyte"],
  chart_measurements: ["measurement", "subject"],
  make_intake_link: ["domain", "fields_brief"],
  create_list: ["title"],
  add_list_item: ["body"],
  manage_appointment: ["action", "title"],
  write_plan: ["status"],
  write_plan_result: ["heading", "note"],
  current_location: ["detail"],
  current_time: ["timezone"],
  location_history: ["subject"],
  location_query: ["place"],
  find_when_at: ["place"],
  time_at_place: ["place"],
  where_is: ["subject"],
  save_place: ["name"],
  geocode_reverse: ["latitude", "longitude"],
  query_server_metrics: ["range"],
  weather: ["location"],
  weather_history: ["location", "start_date"],
  hurricane: ["location"],
  analyze_image: ["prompt"],
  analyze_stream: ["url"],
  compare_images: ["prompt"],
  fetch_image: ["url"],
  grab_frame: ["url", "question"],
  external_video: ["action", "query", "url"],
  show_external_video: ["url"],
  remove_external_video: ["url"],
  render_chart: ["title"],
  render_bars: ["title"],
  render_html: ["caption"],
  show_canvas: ["caption"],
  moltbook: ["action", "query", "name"],
  moltbook_post: ["submolt", "title"],
  moltbook_comment: ["content"],
  moltbook_social: ["action", "name"],
  moltbook_profile_update: ["bio"],
  jmolt_observe: ["action", "filename"],
};

// Tools with nothing legible to put on the row — every argument is an opaque id,
// a boolean, or a structured blob (or there are no arguments at all). An explicit
// opt-out, not an omission: the roster gate requires each tool to appear in
// exactly one of INLINE_ARGS / NO_INLINE.
const NO_INLINE: ReadonlySet<string> = new Set([
  // takes no arguments — there is only one radio to release
  "sdr_stop",
  "read_note",
  "read_entity",
  // D15 injects the standing instructions into the prompt; the read takes no argument.
  "prefs_read",
  "read_wiki",
  "request_rebuild",
  "archivist_memory_read",
  "scratch_list",
  "time_left",
  "gmail_read",
  "gmail_archive",
  "gmail_label",
  "gmail_list_labels",
  "read_encounters",
  "read_list",
  "read_lists",
  "remove_list_item",
  "check_list_item",
  "read_appointment",
  "read_appointments",
  "read_plan",
  "read_artifact",
  "where_was_i",
  "nearby_now",
  "home_status",
  "device_status",
  "analyze_video",
  "crop_regions",
  "ocr",
  "transcribe",
  "canvas",
  "moltbook_vote",
  "spawn_subagent",
  "decompose_research",
]);

// A tool not registered above (a future addition that missed the roster gate, or
// a synthetic step name) still tries the universally human-readable keys, first
// hit wins — a new tool degrades to a sensible inline rather than a bare row.
const FALLBACK_KEYS: readonly string[] = [
  "query",
  "url",
  "question",
  "name",
  "title",
  "place",
  "subject",
  "location",
  "filename",
  "action",
  "prompt",
];

// At most two pieces joined on the row, each clamped so a pasted note body can't
// swallow it (matches the backend child-trace clamp in agent/spawn.py).
const INLINE_PIECES = 2;
const INLINE_PIECE_LEN = 200;
// How many elements of a batched argument name themselves on the row before the rest
// become a "+N". The W3 note-ingest tools take ≤12 surfaces / ≤8 facts per call
// (TOOL_SURFACE.md), so a batch that printed every element would swallow the row.
const INLINE_BATCH = 3;
// The keys a batch ELEMENT is named by, first hit wins — the same "first legible key"
// rule FALLBACK_KEYS applies to a whole argument map, one level down.
const BATCH_ELEMENT_KEYS: readonly string[] = [
  "surface",
  "name",
  "subject",
  "statement",
  "text",
  "value",
];

function scalarPiece(v: unknown): string | undefined {
  if (typeof v === "number") return String(v);
  if (typeof v !== "string" || !v.trim()) return undefined;
  const s = v.trim();
  return s.length > INLINE_PIECE_LEN ? `${s.slice(0, INLINE_PIECE_LEN)}…` : s;
}

// One element of a batched argument: a bare string, or the first legible field of an
// object (the batch shapes W2 measured are arrays of small objects).
function elementPiece(v: unknown): string | undefined {
  const scalar = scalarPiece(v);
  if (scalar) return scalar;
  if (v === null || typeof v !== "object" || Array.isArray(v)) return undefined;
  const o = v as Record<string, unknown>;
  for (const key of BATCH_ELEMENT_KEYS) {
    const piece = scalarPiece(o[key]);
    if (piece) return piece;
  }
  return undefined;
}

function inlinePiece(v: unknown): string | undefined {
  if (!Array.isArray(v)) return scalarPiece(v);
  const named = v.map(elementPiece).filter((p): p is string => p !== undefined);
  if (named.length === 0) return undefined;
  const shown = named.slice(0, INLINE_BATCH).join(", ");
  return named.length > INLINE_BATCH ? `${shown} +${named.length - INLINE_BATCH}` : shown;
}

function inlineArg(name: string, args: Record<string, unknown> | undefined): string | undefined {
  if (!args || NO_INLINE.has(name)) return undefined;
  const keys = INLINE_ARGS[name];
  const pieces: string[] = [];
  for (const key of keys ?? []) {
    const piece = inlinePiece(args[key]);
    if (piece) pieces.push(piece);
    if (pieces.length === INLINE_PIECES) break;
  }
  if (pieces.length > 0) return pieces.join(" · ");
  if (keys) return undefined;
  for (const key of FALLBACK_KEYS) {
    const piece = inlinePiece(args[key]);
    if (piece) return piece;
  }
  return undefined;
}

// "- note <id> [<domain>] <YYYY-MM-DD>: <snippet>"
const SEARCH_LINE = /^- note (\S+) \[(\w+)\] \d{4}-\d{2}-\d{2}: (.*)$/;
// "note <id> [<domain>] <YYYY-MM-DD>" then body on following lines
const NOTE_HEAD = /^note (\S+) \[(\w+)\] \d{4}-\d{2}-\d{2}/;

const stripMarks = (s: string): string => s.replace(/<\/?mark>/g, "").trim();

function searchSources(summary: string): SourceRef[] {
  const out: SourceRef[] = [];
  for (const line of summary.split("\n")) {
    const m = SEARCH_LINE.exec(line);
    if (m?.[1] && m[2]) out.push({ noteId: m[1], domain: m[2], text: stripMarks(m[3] ?? "") });
  }
  return out;
}

function noteSource(summary: string): SourceRef[] {
  const lines = summary.split("\n");
  const m = lines[0] ? NOTE_HEAD.exec(lines[0]) : null;
  if (!m?.[1] || !m[2]) return [];
  const body = lines.slice(1).find((l) => l.trim()) ?? "";
  return [{ noteId: m[1], domain: m[2], text: stripMarks(body) || "(empty note)" }];
}

export function toolStep(t: ToolActivity): ToolStep {
  let sources: SourceRef[];
  if (t.sources && t.sources.length > 0) {
    // Structured from the result event — just strip search highlight marks.
    sources = t.sources.map((s) => ({ ...s, text: stripMarks(s.text) }));
  } else if (t.summary && t.name === "search") {
    sources = searchSources(t.summary);
  } else if (t.summary && t.name === "read_note") {
    sources = noteSource(t.summary);
  } else {
    sources = [];
  }
  return {
    id: t.id,
    name: t.name,
    ok: t.ok,
    label: stepLabel(t.name),
    inline: inlineArg(t.name, t.args),
    sources,
    webSources: t.webSources ?? [],
    entities: t.entities ?? [],
    facts: t.facts ?? [],
    truncated: t.truncated === true,
    args: t.args,
    summary: t.summary,
  };
}
