// The tool-view component registry: a fixed map from a `view` name to a
// first-party React component, and <ToolView> which renders the named component
// from a ViewPayload — or NOTHING if the name is unknown. This is invariant #1/#9
// (DESIGN.md "Agent tool views"): model output never authors markup; it only
// selects a registered component and fills its data-only slots. Adding a
// component is a deliberate change here, like adding a tool.

import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import {
  type MetricPoint,
  attachmentUrl,
  chatAttachmentThumbUrl,
  chatAttachmentUrl,
  generatedImageSourceUrl,
  generatedImageUrl,
} from "../../api/client";
import { AudioTranscript, transcriptWords } from "../../components/AudioTranscript";
import {
  type ChartPoint,
  InteractiveChart,
  type PointFlag,
} from "../../components/InteractiveChart";
import { TaskStatus } from "../../components/TaskStatus";
import { TimeSeriesPlot } from "../../components/TimeSeriesPlot";
import { VideoAnalysis, type VideoFrame } from "../../components/VideoAnalysis";
import { serverMetricSeries } from "../../components/serverMetricSeries";
import { Markdown } from "../markdown";
import type { CitationRef, ViewPayload } from "../types";
import { Lightbox } from "./Lightbox";
import {
  type HuGeoPoint,
  type HuMapData,
  type HuTrackPointGeo,
  renderHurricaneMap,
} from "./hurricaneMap";
import {
  type LiveList,
  getLiveList,
  loadLiveList,
  seedLiveList,
  subscribeLiveLists,
  toggleLiveItem,
} from "./liveList";
import { type InlineMapHandle, type TrailLegData, renderPlace, renderTrail } from "./locationMap";

export interface ViewProps {
  data: Record<string, unknown>;
  refs: CitationRef[];
  /** Open an agent session by id — used by the sub-agent synthesis card to deep-link
   * each child row to its own session. Most views ignore it. */
  onOpenSession?: ((sessionId: string) => void) | undefined;
  /** A deferred tool call finished — the task_status card calls this once (with the
   * server-authored result report) so the controller sends the auto-resume turn. Only
   * the task_status view uses it (DEFERRED_TOOL_CALLS_PLAN.md P3). */
  onDeferredComplete?: ((resumeMessage: string) => void) | undefined;
}

// Tone/flag is an enum, never a color (DESIGN.md): the component maps it to a
// class so the theme owns the palette.
type Tone = "neutral" | "good" | "warn" | "bad";
const TONES = new Set<Tone>(["neutral", "good", "warn", "bad"]);
function toneOf(value: unknown): Tone {
  return typeof value === "string" && TONES.has(value as Tone) ? (value as Tone) : "neutral";
}

function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

/** A simple labelled figure: `{label, value, unit?, tone?}`. */
function StatBlock({ data }: ViewProps): ReactNode {
  const unit = typeof data.unit === "string" ? data.unit : "";
  return (
    <div className={`tv-stat tone-${toneOf(data.tone)}`}>
      <div className="tv-stat-value">
        {String(data.value ?? "")}
        {unit && <span className="tv-stat-unit">{unit}</span>}
      </div>
      <div className="tv-stat-label">{String(data.label ?? "")}</div>
    </div>
  );
}

/** A read-only grid: `{columns: string[], rows: string[][]}`. */
function DataTable({ data }: ViewProps): ReactNode {
  const columns = asStrings(data.columns);
  const rows = Array.isArray(data.rows) ? data.rows : [];
  return (
    <table className="tv-table">
      {columns.length > 0 && (
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
      )}
      <tbody>
        {rows.map((row, r) => (
          // Rows are positional data with no stable id; index is the only key.
          // biome-ignore lint/suspicious/noArrayIndexKey: positional table rows
          <tr key={r}>
            {asStrings(row).map((cell, c) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: positional cells
              <td key={c}>{cell}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

interface ChecklistItem {
  id: string;
  body: string;
  checked: boolean;
}

function asItems(value: unknown): ChecklistItem[] {
  if (!Array.isArray(value)) return [];
  return value.map((it) => {
    const o = (it ?? {}) as Record<string, unknown>;
    return { id: String(o.id ?? ""), body: String(o.body ?? ""), checked: Boolean(o.checked) };
  });
}

/** The owner's checklist: `{list_id, title, items: [{id, body, checked}]}` —
 * full-bleed rows (DESIGN.md "Lists"). Reads LIVE list state from the shared
 * store keyed on `list_id`, so an older card and a newer one of the same list
 * always agree, and a checkbox tap (here, in another card, or after the agent
 * edits it) reflects everywhere. Optimistic, reverting if the write fails. */
function ListCard({ data }: ViewProps): ReactNode {
  const listId = String(data.list_id ?? "");
  const fallback: LiveList = {
    title: String(data.title ?? "List"),
    domain: String(data.domain ?? "general"),
    items: asItems(data.items),
  };
  const [live, setLive] = useState<LiveList>(() => getLiveList(listId) ?? fallback);

  // Keyed on the list id only: the payload `fallback` seeds the store the first
  // time and is otherwise stable, so it's deliberately out of the dep array.
  // biome-ignore lint/correctness/useExhaustiveDependencies: payload-derived seed, id-keyed
  useEffect(() => {
    if (!listId) return;
    seedLiveList(listId, fallback);
    const got = getLiveList(listId);
    if (got) setLive(got);
    const unsub = subscribeLiveLists(() => {
      const next = getLiveList(listId);
      if (next) setLive(next);
    });
    void loadLiveList(listId); // pull the current state → emits → setLive
    return unsub;
  }, [listId]);

  const items = live.items;

  function toggle(target: ChecklistItem): void {
    if (listId) {
      toggleLiveItem(listId, target.id, !target.checked);
      return;
    }
    // No list id (shouldn't happen for a real list_card) — local-only.
    setLive((l) => ({
      ...l,
      items: l.items.map((x) => (x.id === target.id ? { ...x, checked: !target.checked } : x)),
    }));
  }

  return (
    <div className="tv-list">
      <div className="tv-list-head">{live.title}</div>
      <ul className="tv-list-items">
        {items.map((it, i) => (
          // Item ids are stable; the index only backs the rare empty-id row.
          <li key={it.id || i} className={`tv-list-row${it.checked ? " checked" : ""}`}>
            <button
              type="button"
              className="tv-list-check"
              aria-pressed={it.checked}
              aria-label={`${it.checked ? "Uncheck" : "Check"} ${it.body}`}
              onClick={() => toggle(it)}
            >
              <span className="tv-list-box" aria-hidden="true" />
            </button>
            <span className="tv-list-body">{it.body}</span>
          </li>
        ))}
        {items.length === 0 && <li className="tv-list-empty">empty</li>}
      </ul>
    </div>
  );
}

function fmtWhen(data: Record<string, unknown>): string {
  const start = typeof data.start === "string" ? data.start : "";
  if (!start) return "";
  const d = new Date(start);
  if (Number.isNaN(d.getTime())) return start;
  if (data.all_day) {
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }
  const when = d.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
  const rawEnd = typeof data.end === "string" ? data.end : "";
  const end = rawEnd ? new Date(rawEnd) : null;
  if (end && !Number.isNaN(end.getTime())) {
    return `${when}–${end.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
  }
  return when;
}

// The appointment.yaml Lifecycle enum; the component maps it to a flag class so
// the theme owns the palette (DESIGN.md: status is a flag enum, never a color).
const APPT_STATUSES = new Set(["tentative", "confirmed", "cancelled", "occurred"]);
function apptStatus(value: unknown): string {
  return typeof value === "string" && APPT_STATUSES.has(value) ? value : "confirmed";
}

/** One appointment: `{title, start, end?, all_day, status, location?, rrule?,
 * recurring, attendees: string[]}` — a read-only card projected from the owner's
 * notes (the manage actions land in P4 PR4). Times localize to the owner's zone;
 * status is a flag enum the theme colors. */
function AppointmentCard({ data }: ViewProps): ReactNode {
  const status = apptStatus(data.status);
  const location = typeof data.location === "string" ? data.location : "";
  const attendees = asStrings(data.attendees);
  return (
    <div className={`tv-appt status-${status}`}>
      <div className="tv-appt-head">
        <span className="tv-appt-title">{String(data.title ?? "Appointment")}</span>
        <span className={`tv-appt-status flag-${status}`}>{status}</span>
      </div>
      <div className="tv-appt-when">{fmtWhen(data)}</div>
      {location && <div className="tv-appt-loc">{location}</div>}
      {data.recurring ? <div className="tv-appt-repeat">repeats</div> : null}
      {attendees.length > 0 && <div className="tv-appt-with">with {attendees.join(", ")}</div>}
    </div>
  );
}

function refKey(ref: CitationRef): string {
  if (ref.kind === "fact") return `fact:${ref.fact_id}`;
  if (ref.kind === "entity") return `entity:${ref.entity_id}`;
  return `note:${ref.note_id}`;
}

/** Pointer-not-copy citation chips from the payload's refs (hover-cards later). */
function CitationCard({ data, refs }: ViewProps): ReactNode {
  return (
    <div className="tv-citations">
      {typeof data.title === "string" && <div className="tv-citations-title">{data.title}</div>}
      <div className="tv-citation-chips">
        {refs.map((ref) => (
          <span key={refKey(ref)} className={`tv-cite kind-${ref.kind}`}>
            {ref.label}
          </span>
        ))}
      </div>
    </div>
  );
}

// --- location views (the first Leaflet-dependent tool-views) ----------------
//
// Coordinates are render-only: lat/lon enter only through the map glue
// (`renderTrail`/`renderPlace`), never as text in the bubble. Times localize to
// the payload's `timezone`; the gap is explained in words by the segments list
// BEFORE the map is opened (DESIGN.md Option B answer-first).

interface MapLeg extends TrailLegData {
  fix_count: number;
  started_at: string;
  ended_at: string;
  distance_m: number;
}
interface MapGap {
  after_leg: number;
  started_at: string;
  ended_at: string;
  seconds: number;
}

function asLegs(value: unknown): MapLeg[] {
  if (!Array.isArray(value)) return [];
  return value.map((leg) => {
    const o = (leg ?? {}) as Record<string, unknown>;
    const rawPoints = Array.isArray(o.points) ? o.points : [];
    const points = rawPoints
      .map((p) => (Array.isArray(p) ? [Number(p[0]), Number(p[1])] : [Number.NaN, Number.NaN]))
      .filter((p): p is [number, number] => !Number.isNaN(p[0]) && !Number.isNaN(p[1]));
    return {
      points,
      fix_count: Number(o.fix_count ?? 0),
      started_at: String(o.started_at ?? ""),
      ended_at: String(o.ended_at ?? ""),
      distance_m: Number(o.distance_m ?? 0),
    };
  });
}

function asGaps(value: unknown): MapGap[] {
  if (!Array.isArray(value)) return [];
  return value.map((gap) => {
    const o = (gap ?? {}) as Record<string, unknown>;
    return {
      after_leg: Number(o.after_leg ?? 0),
      started_at: String(o.started_at ?? ""),
      ended_at: String(o.ended_at ?? ""),
      seconds: Number(o.seconds ?? 0),
    };
  });
}

function fmtTime(iso: string, tz: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const opts: Intl.DateTimeFormatOptions = {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  };
  if (tz) opts.timeZone = tz;
  return d.toLocaleString(undefined, opts);
}

function fmtKm(m: number): string {
  return `${(m / 1000).toFixed(1)} km`;
}

function fmtGap(seconds: number): string {
  const h = seconds / 3600;
  if (h >= 1) return `~${Math.round(h)} h gap`;
  return `~${Math.round(seconds / 60)} min gap`;
}

/** location_map (#3) — Option B answer-first: a tap-to-expand map thumbnail with
 * a text segments list naming each leg + the gap between them. Coordinates render
 * only inside the Leaflet layers (via the mocked `renderTrail`); an empty window
 * shows a "no trail" placeholder, never a blank map. */
function LocationMap({ data }: ViewProps): ReactNode {
  const tz = typeof data.timezone === "string" ? data.timezone : null;
  // Memoize the parsed legs/gaps so they're stable identities the effects can
  // depend on (the payload is immutable for the card's life; this just avoids a
  // fresh array each render driving a redraw).
  const legs = useMemo(() => asLegs(data.legs), [data.legs]);
  const gaps = useMemo(() => asGaps(data.gaps), [data.gaps]);
  const freshness = typeof data.freshness === "string" ? data.freshness : "";
  const freshLabel = typeof data.fresh_label === "string" ? data.fresh_label : "";
  const [open, setOpen] = useState(false);
  const thumbRef = useRef<HTMLDivElement>(null);
  const fullRef = useRef<HTMLDivElement>(null);

  const hasTrail = legs.some((l) => l.points.length > 0);

  // Draw the (static) thumbnail once the trail is present. The expanded map is
  // drawn lazily when first opened so a collapsed card costs no second Leaflet.
  useEffect(() => {
    if (!hasTrail || !thumbRef.current) return;
    const handle = renderTrail(thumbRef.current, legs, { interactive: false });
    return () => handle.destroy();
  }, [legs, hasTrail]);

  useEffect(() => {
    if (!open || !fullRef.current) return;
    let handle: InlineMapHandle | null = renderTrail(fullRef.current, legs, { interactive: true });
    // Leaflet mis-measures inside a just-expanded box; re-measure next frame.
    const t = setTimeout(() => handle?.invalidate(), 60);
    return () => {
      clearTimeout(t);
      handle?.destroy();
      handle = null;
    };
  }, [open, legs]);

  if (!hasTrail) {
    return <div className="loc-map-empty">No location in this window.</div>;
  }

  // The segments list interleaves legs with the gaps that follow them, so the
  // "no signal" row sits between the two legs it separates.
  const rows: ReactNode[] = [];
  legs.forEach((leg, i) => {
    rows.push(
      // Keyed on the leg's start time (stable + unique per leg); the ordinal in
      // the title is display-only.
      <div className="loc-seg" key={`leg-${leg.started_at}`}>
        <span className="loc-seg-knob" />
        <div>
          <div className="loc-seg-title">
            Leg {i + 1}
            {leg.fix_count ? ` · ${leg.fix_count} fixes` : ""}
          </div>
          <div className="loc-seg-meta">
            {fmtTime(leg.started_at, tz)} – {fmtTime(leg.ended_at, tz)} · {fmtKm(leg.distance_m)}
          </div>
        </div>
      </div>,
    );
    const gap = gaps.find((g) => g.after_leg === i);
    if (gap) {
      rows.push(
        <div className="loc-seg gap" key={`gap-${gap.started_at}`}>
          <span className="loc-seg-knob" />
          <div>
            <div className="loc-seg-title">No signal — route unknown</div>
            <div className="loc-seg-meta">
              {fmtTime(gap.started_at, tz)} – {fmtTime(gap.ended_at, tz)} · {fmtGap(gap.seconds)} ·
              not drawn across
            </div>
          </div>
        </div>,
      );
    }
  });

  return (
    <div className="loc-map">
      <button
        type="button"
        className="loc-map-thumb"
        aria-label={open ? "Collapse map" : "Expand map"}
        onClick={() => setOpen((v) => !v)}
      >
        <div className="loc-map-canvas" ref={thumbRef} />
        <div className="loc-map-overlay">
          {freshLabel && (
            <span className={`loc-map-pill ${freshness === "stale" ? "stale" : "fresh"}`}>
              <span className="loc-map-dot" />
              {freshLabel}
            </span>
          )}
          <span className="loc-map-pill">{open ? "tap to collapse" : "tap to explore"}</span>
        </div>
      </button>
      <div className={`loc-map-full${open ? " open" : ""}`}>
        {open && <div className="loc-map-canvas" ref={fullRef} />}
      </div>
      <div className="loc-segs">
        <div className="loc-segs-head">
          {legs.length} leg{legs.length === 1 ? "" : "s"}
          {gaps.length ? ` · split at ${gaps.length} gap${gaps.length === 1 ? "" : "s"}` : ""}
        </div>
        {rows}
      </div>
    </div>
  );
}

interface PlaceChip {
  label: string;
  kind: string;
}
function asChips(value: unknown): PlaceChip[] {
  if (!Array.isArray(value)) return [];
  return value.map((c) => {
    const o = (c ?? {}) as Record<string, unknown>;
    return { label: String(o.label ?? ""), kind: String(o.kind ?? "thing") };
  });
}

/** place_card (#4) — a dense one-row place dossier: a mini-map beside the
 * title/address/stats. The address is a NAME, never coordinates (the centre
 * reaches only the mocked `renderPlace`). Derived stats are OWNER-GATED: a
 * payload without `owner: true` omits the whole stat block (a narrowed/non-owner
 * session never sees visit counts). Entity chips are note-sourced. */
function PlaceCard({ data }: ViewProps): ReactNode {
  const mapRef = useRef<HTMLDivElement>(null);
  const center = Array.isArray(data.center) ? data.center : null;
  const lat = center ? Number(center[0]) : Number.NaN;
  const lon = center ? Number(center[1]) : Number.NaN;
  const hasCenter = !Number.isNaN(lat) && !Number.isNaN(lon);
  const radius = typeof data.radius_m === "number" ? data.radius_m : null;
  const owner = data.owner === true;
  const stats = owner && Array.isArray(data.stats) ? data.stats : [];
  const chips = asChips(data.chips);

  useEffect(() => {
    if (!hasCenter || !mapRef.current) return;
    const handle = renderPlace(mapRef.current, [lat, lon], radius);
    return () => handle.destroy();
  }, [hasCenter, lat, lon, radius]);

  return (
    <div className="loc-pc-card">
      <div className="loc-pc">
        {hasCenter && <div className="loc-pc-mini" ref={mapRef} />}
        <div className="loc-pc-main">
          <div className="loc-pc-title">{String(data.name ?? "Place")}</div>
          {typeof data.address === "string" && data.address && (
            <div className="loc-pc-addr">{data.address}</div>
          )}
          {owner && stats.length > 0 && (
            <div className="loc-pc-stats">
              {stats.map((s) => {
                const o = (s ?? {}) as Record<string, unknown>;
                const label = String(o.label ?? "");
                return (
                  <div className="loc-pc-stat" key={label}>
                    <div className="loc-pc-num">{String(o.value ?? "")}</div>
                    <div className="loc-pc-lbl">{label}</div>
                  </div>
                );
              })}
            </div>
          )}
          {owner && stats.length > 0 && <div className="loc-pc-owner">owner-only stats</div>}
        </div>
      </div>
      {chips.length > 0 && (
        <>
          <div className="loc-pc-chips">
            {chips.map((c) => (
              <span className={`loc-pc-chip kind-${c.kind}`} key={`${c.kind}:${c.label}`}>
                {c.label}
              </span>
            ))}
          </div>
          <div className="loc-pc-chipnote">From notes about this place.</div>
        </>
      )}
    </div>
  );
}

// generated_image (#5) — the image-gen tool's in-chat card. Data-only slots:
// {image_id, kind, prompt, width, height, model}. The component BUILDS the
// <img> srcs from image_id (invariant #9: never a model-authored URL) and sizes
// the frame from width/height to avoid layout shift. A `generate` renders a
// single sized image; an `edit` renders the source→result as a swipe compare
// (before = the source bytes resolved by id, after = the result) plus a
// Compare/Before/After toggle for accessibility.

// Clamp a parsed dimension to a sane positive integer (a 0 or NaN would make the
// frame's aspect-ratio collapse); falls back to a square so layout is stable.
function asDim(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.round(n) : 512;
}

// The before/after wipe: drag (or the grip) reveals the source under the edited
// result. Just the slider now — the Compare/Edited mode switch lives in EditView.
// Exported so the image-launcher screen reuses the same swipe-compare rather than
// duplicating it (the .tv-genimg-* classes work in either scope, see styles.css).
export function EditCompare({
  beforeSrc,
  afterSrc,
  width,
  height,
  alt,
}: {
  beforeSrc: string;
  afterSrc: string;
  width: number;
  height: number;
  alt: string;
}): ReactNode {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState(50); // wipe position, 0–100 (% from the left)
  const dragging = useRef(false);

  function moveTo(clientX: number): void {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0) return;
    setPos(Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100)));
  }

  return (
    <div
      ref={ref}
      className="tv-genimg-cmp"
      style={{ aspectRatio: `${width} / ${height}`, ["--pos" as string]: `${pos}%` }}
      onPointerDown={(e) => {
        dragging.current = true;
        e.currentTarget.setPointerCapture(e.pointerId);
        moveTo(e.clientX);
      }}
      onPointerMove={(e) => {
        if (dragging.current) moveTo(e.clientX);
      }}
      onPointerUp={() => {
        dragging.current = false;
      }}
      onPointerCancel={() => {
        dragging.current = false;
      }}
    >
      <img
        className="tv-genimg-img"
        src={beforeSrc}
        alt={`Before: ${alt}`}
        width={width}
        height={height}
      />
      <div className="tv-genimg-after">
        <img
          className="tv-genimg-img"
          src={afterSrc}
          alt={`After: ${alt}`}
          width={width}
          height={height}
        />
      </div>
      <span className="tv-genimg-lbl b">BEFORE</span>
      <span className="tv-genimg-lbl a">AFTER</span>
      <div className="tv-genimg-handle" aria-hidden="true" />
      <div className="tv-genimg-grip" aria-hidden="true" />
    </div>
  );
}

// The clickable image frame shared by a generated image and an edit's "Edited" view:
// the picture fills the frame, fades in over its placeholder/skeleton, and opens the
// full-screen pinch/zoom viewer on tap. Exported for the image-launcher screen's
// result + gallery lightbox (same reuse note as EditCompare).
export function ImageFrame({
  src,
  alt,
  width,
  height,
  placeholder = "",
}: {
  src: string;
  alt: string;
  width: number;
  height: number;
  placeholder?: string;
}): ReactNode {
  const [loaded, setLoaded] = useState(false);
  const [zoom, setZoom] = useState(false);
  return (
    <>
      <button
        type="button"
        className="tv-genimg-frame"
        style={{ aspectRatio: `${width} / ${height}` }}
        onClick={() => setZoom(true)}
        aria-label="Expand image to full screen"
      >
        {!loaded &&
          (placeholder ? (
            <img className="tv-genimg-ph" src={placeholder} alt="" aria-hidden="true" />
          ) : (
            <div className="tv-genimg-skeleton" />
          ))}
        <img
          className="tv-genimg-img"
          src={src}
          alt={alt}
          width={width}
          height={height}
          onLoad={() => setLoaded(true)}
          style={{ opacity: loaded ? 1 : 0 }}
        />
        <span className="tv-genimg-expand" aria-hidden="true">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M9 3H5a2 2 0 0 0-2 2v4M15 3h4a2 2 0 0 1 2 2v4M9 21H5a2 2 0 0 1-2-2v-4M15 21h4a2 2 0 0 0 2-2v-4" />
          </svg>
        </span>
      </button>
      {zoom && <Lightbox src={src} alt={alt} onClose={() => setZoom(false)} />}
    </>
  );
}

function GeneratedImage({ data }: ViewProps): ReactNode {
  const imageId = String(data.image_id ?? "");
  const kind = data.kind === "edit" ? "edit" : "generate";
  const prompt = typeof data.prompt === "string" ? data.prompt : "";
  const width = asDim(data.width);
  const height = asDim(data.height);
  const model = typeof data.model === "string" ? data.model : "";
  const seed = typeof data.seed === "number" ? data.seed : null;
  const alt = prompt || "Generated image";
  // Surface the seed on the card so the owner can see it and ask to reuse it.
  const meta = `${width} × ${height}${seed !== null ? ` · seed ${seed}` : ""}${
    model ? ` · ${model}` : ""
  }`;

  if (kind === "edit") {
    return (
      <EditView
        beforeSrc={generatedImageSourceUrl(imageId)}
        afterSrc={generatedImageUrl(imageId)}
        width={width}
        height={height}
        alt={alt}
        meta={meta}
      />
    );
  }

  return <GenerateImage src={generatedImageUrl(imageId)} alt={alt} meta={meta} data={data} />;
}

/** `{attachment_id, source, media?, filename, model?, duration_ms?, words: [...]}` —
 * the media-transcript card. The component builds the media src from the id +
 * source (a chat attachment for jerv's tool, a note attachment otherwise) and
 * renders a <video> when `media` is "video", else an <audio> player; no URL rides
 * the payload (invariant #9). */
function Transcript({ data }: ViewProps): ReactNode {
  const attachmentId = String(data.attachment_id ?? "");
  const source = data.source === "note" ? "note" : "chat";
  const audioUrl =
    source === "note" ? attachmentUrl(attachmentId) : chatAttachmentUrl(attachmentId);
  return (
    <AudioTranscript
      audioUrl={audioUrl}
      media={data.media === "video" ? "video" : "audio"}
      filename={typeof data.filename === "string" ? data.filename : "audio"}
      model={typeof data.model === "string" ? data.model : undefined}
      durationMs={typeof data.duration_ms === "number" ? data.duration_ms : null}
      words={transcriptWords(data.words)}
      text={typeof data.text === "string" ? data.text : undefined}
    />
  );
}

/** Map the tool-view frame shape ({t_ms, caption, thumb_id?, thumb_data_uri?}) to the
 * card's props. An attachment frame builds its thumbnail src from its blob id via
 * `thumbUrl` (the backend validates the id against the attachment's stored frames under
 * the firewall); a stream frame — which has no served route — carries a small inline
 * `thumb_data_uri` the server built (no external fetch, #9), used as the src directly. A
 * frame with neither renders without a still. */
function videoFrames(value: unknown, thumbUrl: ((thumbId: string) => string) | null): VideoFrame[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((f): VideoFrame[] => {
    if (typeof f !== "object" || f === null) return [];
    const o = f as Record<string, unknown>;
    if (typeof o.caption !== "string") return [];
    const inline = typeof o.thumb_data_uri === "string" ? o.thumb_data_uri : undefined;
    const built = thumbUrl && typeof o.thumb_id === "string" ? thumbUrl(o.thumb_id) : undefined;
    return [
      {
        tMs: typeof o.t_ms === "number" ? o.t_ms : 0,
        caption: o.caption,
        thumbUrl: inline ?? built,
      },
    ];
  });
}

/** `{attachment_id?, source, filename, summary, duration_ms, frames:[{t_ms, caption,
 * thumb_id}], transcript:{text, words}|null, stream_url?, is_live?}` — the analyze_video
 * thumb_id?, thumb_data_uri?}], transcript:{text, words}|null, stream_url?, is_live?}` —
 * the analyze_video / analyze_stream card. An attachment source builds media + thumbnail
 * srcs from the id (a chat attachment for jerv's tool, a note attachment otherwise); a
 * `stream` source (analyze_stream) has no attachment, so it renders no <video> and its
 * frames carry a small server-built inline `thumb_data_uri` (no external fetch, #9); a
 * YouTube stream also carries `youtube_id`, which the card embeds as a player synced to
 * the timeline (the #9 exception). No URL rides the payload. */
function VideoAnalysisView({ data }: ViewProps): ReactNode {
  const attachmentId = String(data.attachment_id ?? "");
  const source = data.source === "note" ? "note" : data.source === "stream" ? "stream" : "chat";
  // A stream (analyze_stream) has no playable local attachment and no served thumbnail
  // route — the card drops the <video> (embedding the YouTube player instead when the
  // source is YouTube) and renders frames from their inline thumb. A note/chat
  // attachment builds its media + thumb srcs from the id (no URL rides the payload,
  // invariant #9); thumbnails are served only for chat attachments.
  const videoUrl =
    source === "stream"
      ? undefined
      : source === "note"
        ? attachmentUrl(attachmentId)
        : chatAttachmentUrl(attachmentId);
  const youtubeId =
    source === "stream" && typeof data.youtube_id === "string" && data.youtube_id
      ? data.youtube_id
      : undefined;
  // A stream carries its liveness + page URL for the header LIVE badge and source chip
  // (data, not a rendered resource — the chip is a user-tapped link, #9).
  const isLive = source === "stream" && data.is_live === true;
  const sourceUrl =
    source === "stream" && typeof data.stream_url === "string" && data.stream_url
      ? data.stream_url
      : undefined;
  const thumbUrl =
    source === "chat" ? (id: string) => chatAttachmentThumbUrl(attachmentId, id) : null;
  const transcript =
    data.transcript && typeof data.transcript === "object"
      ? (data.transcript as Record<string, unknown>)
      : null;
  return (
    <VideoAnalysis
      videoUrl={videoUrl}
      youtubeId={youtubeId}
      isLive={isLive}
      sourceUrl={sourceUrl}
      filename={typeof data.filename === "string" ? data.filename : "video"}
      summary={typeof data.summary === "string" ? data.summary : ""}
      frames={videoFrames(data.frames, thumbUrl)}
      words={transcriptWords(transcript?.words)}
      transcriptText={typeof transcript?.text === "string" ? transcript.text : undefined}
      transcriptSource={
        typeof data.transcript_source === "string" ? data.transcript_source : undefined
      }
    />
  );
}

/** `{result_id, result_view, title, ...}` — the task_status card for a deferred tool call
 * (DEFERRED_TOOL_CALLS_PLAN.md P3). It polls the background job's progress and, on
 * completion, swaps to the result view named by `result_view` (today always
 * video_analysis — the analyze_stream deferral is the first adopter). The result data the
 * job stored is the same video_analysis payload the in-turn card gets, so the swap is
 * seamless. Reusable: a future deferred tool adds a branch for its own result view. */
function TaskStatusView({ data, onDeferredComplete }: ViewProps): ReactNode {
  const resultId = typeof data.result_id === "string" ? data.result_id : "";
  const title = typeof data.title === "string" ? data.title : "Working…";
  if (!resultId) return null;
  return (
    <TaskStatus
      resultId={resultId}
      title={title}
      renderResult={(result) => <VideoAnalysisView data={result} refs={[]} />}
      onComplete={onDeferredComplete}
    />
  );
}

function GenerateImage({
  src,
  alt,
  meta,
  data,
}: {
  src: string;
  alt: string;
  meta: string;
  data: Record<string, unknown>;
}): ReactNode {
  // The last live preview frame, handed down from the in-flight turn (live only, absent
  // on reopen): held as the placeholder until the full-res image loads, so there's no
  // blank gap between "finalizing" and the rendered image.
  const placeholder =
    typeof data.placeholder_data_uri === "string" ? data.placeholder_data_uri : "";
  return (
    <div className="tv-genimg">
      <ImageFrame
        src={src}
        alt={alt}
        width={asDim(data.width)}
        height={asDim(data.height)}
        placeholder={placeholder}
      />
      <div className="tv-genimg-cap">{meta}</div>
    </div>
  );
}

// An edit result with two modes: Compare (the before/after wipe) and Edited (just the
// new picture, click to pinch/zoom like a generated image). The owner wanted out of
// the slider into a plain, zoomable view of the result.
function EditView({
  beforeSrc,
  afterSrc,
  width,
  height,
  alt,
  meta,
}: {
  beforeSrc: string;
  afterSrc: string;
  width: number;
  height: number;
  alt: string;
  meta: string;
}): ReactNode {
  const [mode, setMode] = useState<"compare" | "edited">("compare");
  return (
    <div className="tv-genimg">
      {mode === "compare" ? (
        <EditCompare
          beforeSrc={beforeSrc}
          afterSrc={afterSrc}
          width={width}
          height={height}
          alt={alt}
        />
      ) : (
        <ImageFrame src={afterSrc} alt={alt} width={width} height={height} />
      )}
      <div className="tv-genimg-acts">
        <span className="tv-genimg-cap">{meta}</span>
        {/* Icon-only toggle: text labels overflowed and wrapped the row; the glyphs
            keep the segment to a fixed width so the meta caption can take the rest. */}
        {/* biome-ignore lint/a11y/useSemanticElements: a 2-button toggle group; a <fieldset> is overkill */}
        <div className="tv-genimg-seg" role="group" aria-label="Edit view">
          <button
            type="button"
            className={mode === "compare" ? "on" : ""}
            aria-pressed={mode === "compare"}
            aria-label="Compare"
            title="Compare"
            onClick={() => setMode("compare")}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <rect x="3" y="5" width="18" height="14" rx="2" />
              <path d="M12 5v14" />
              <path className="fill" d="M5 6h6v12H5z" />
            </svg>
          </button>
          <button
            type="button"
            className={mode === "edited" ? "on" : ""}
            aria-pressed={mode === "edited"}
            aria-label="Edited"
            title="Edited"
            onClick={() => setMode("edited")}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 20h9" />
              <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}

/** Host-metrics history (the `query_server_metrics` tool's view): the same
 * sparkline stack the Ops screen draws, from data-only points
 * (`{range, resolution, points: MetricPoint[]}`). */
function ServerMetrics({ data }: ViewProps): ReactNode {
  const points = (Array.isArray(data.points) ? data.points : []) as MetricPoint[];
  const range = typeof data.range === "string" ? data.range : "";
  const resolution = data.resolution === "hourly" ? "hourly" : "30s";
  if (points.length === 0) {
    return <p className="tv-metrics-empty">No host-metrics samples recorded.</p>;
  }
  return (
    <div className="tv-metrics">
      <div className="tv-metrics-head">
        <span>Server health{range ? ` · ${range}` : ""}</span>
        <span className="tv-metrics-sub">{`${points.length} ${resolution} buckets`}</span>
      </div>
      <TimeSeriesPlot series={serverMetricSeries(points)} />
    </div>
  );
}

// --- weather_card ----------------------------------------------------------
// jerv's in-chat forecast (docs/reference/DESIGN.md "weather_card tool-view", variant A —
// hero + hourly strip). Data-only slots, no URLs (#9); `cond` is a closed enum and
// `is_day` a flag the component maps to an inline glyph — the model never sends a
// glyph, an icon URL, or a color.

type WxCond = "clear" | "partly" | "cloudy" | "rain" | "storm" | "snow" | "fog";
const WX_CONDS = new Set<WxCond>(["clear", "partly", "cloudy", "rain", "storm", "snow", "fog"]);
function wxCond(value: unknown): WxCond {
  return typeof value === "string" && WX_CONDS.has(value as WxCond) ? (value as WxCond) : "cloudy";
}
function wxNum(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? Math.round(n) : 0;
}

/** A condition glyph drawn inline (no fetched icons, #9). `day` picks the night
 * variant for clear/partly skies; the other conditions read the same day or night. */
function WeatherGlyph({ cond, day }: { cond: WxCond; day: boolean }): ReactNode {
  if (!day && cond === "clear") {
    return (
      <svg viewBox="0 0 24 24" className="tv-wx-svg" aria-hidden="true">
        <path d="M20 14.5A8 8 0 1 1 9.5 4 6.3 6.3 0 0 0 20 14.5z" />
      </svg>
    );
  }
  if (!day && cond === "partly") {
    return (
      <svg viewBox="0 0 24 24" className="tv-wx-svg" aria-hidden="true">
        <path d="M15.5 5.2A5 5 0 0 0 9 8.5" />
        <path d="M7 18h9a3.2 3.2 0 0 0 .2-6.4 4.3 4.3 0 0 0-8-1.2A3.4 3.4 0 0 0 7 18z" />
      </svg>
    );
  }
  const paths: Record<WxCond, ReactNode> = {
    clear: (
      <>
        <circle cx="12" cy="12" r="4.4" />
        <path d="M12 3v1.5M12 19.5V21M4.5 12H3M21 12h-1.5M6.4 6.4l-1-1M18.6 18.6l-1-1M17.6 6.4l1-1M5.4 18.6l1-1" />
      </>
    ),
    partly: (
      <>
        <path d="M8 6a3 3 0 0 1 5.4 1" />
        <path d="M12 3.5v1M5.6 6.4l-.7-.7M17.5 6l.7-.7" />
        <path d="M7 18h9a3.2 3.2 0 0 0 .2-6.4 4.3 4.3 0 0 0-8-1.2A3.4 3.4 0 0 0 7 18z" />
      </>
    ),
    cloudy: <path d="M7 18h10a3.4 3.4 0 0 0 .3-6.8 4.6 4.6 0 0 0-8.8-1.2A3.6 3.6 0 0 0 7 18z" />,
    rain: (
      <>
        <path d="M7 15h9a3.2 3.2 0 0 0 .3-6.4 4.3 4.3 0 0 0-8.2-1.1A3.4 3.4 0 0 0 7 15z" />
        <path d="M8 18l-1 2.5M12 18l-1 2.5M16 18l-1 2.5" />
      </>
    ),
    storm: (
      <>
        <path d="M7 15h9a3.2 3.2 0 0 0 .3-6.4 4.3 4.3 0 0 0-8.2-1.1A3.4 3.4 0 0 0 7 15z" />
        <path d="M12 16l-2 3.5h3L11 23" />
      </>
    ),
    snow: (
      <>
        <path d="M7 15h9a3.2 3.2 0 0 0 .3-6.4 4.3 4.3 0 0 0-8.2-1.1A3.4 3.4 0 0 0 7 15z" />
        <path d="M9 19h.01M12 20.5h.01M15 19h.01" />
      </>
    ),
    fog: (
      <>
        <path d="M7 13h10a3.4 3.4 0 0 0 .3-6.8 4.6 4.6 0 0 0-8.8-1.2A3.6 3.6 0 0 0 7 13z" />
        <path d="M5 17h14M7 20h10" />
      </>
    ),
  };
  return (
    <svg viewBox="0 0 24 24" className="tv-wx-svg" aria-hidden="true">
      {paths[cond]}
    </svg>
  );
}

/** A drop glyph for the precip-chance line under an hour. */
function DropGlyph(): ReactNode {
  return (
    <svg viewBox="0 0 24 24" className="tv-wx-drop" aria-hidden="true">
      <path d="M12 3s5 6 5 10a5 5 0 0 1-10 0c0-4 5-10 5-10z" />
    </svg>
  );
}

interface WxHour {
  label: string;
  temp_f: number;
  cond: WxCond;
  is_day: boolean;
  pop: number;
}

interface WxDay {
  label: string;
  cond: WxCond;
  hi_f: number;
  lo_f: number;
  pop: number;
}

/** The week card's daily list: one row per day with a temp-range bar scaled to the
 * week's own min/max, so the warm and cool days read at a glance. */
function DailyList({ days }: { days: WxDay[] }): ReactNode {
  const min = Math.min(...days.map((d) => d.lo_f));
  const max = Math.max(...days.map((d) => d.hi_f));
  const span = max - min || 1;
  return (
    <div className="tv-wx-days">
      {days.map((d, i) => {
        // Every bar shares the week's minimum as a common left baseline and runs
        // out to that day's high, so a longer bar means a hotter day — daily
        // differences read at a glance instead of floating at varied offsets.
        const width = ((d.hi_f - min) / span) * 100;
        return (
          // Positional daily rows have no stable id; the day label + index key it.
          <div className="tv-wx-day" key={`${d.label}-${i}`}>
            <div className="tv-wx-dlabel">{d.label}</div>
            <WeatherGlyph cond={d.cond} day={true} />
            <div className={`tv-wx-dpop${d.pop > 0 ? "" : " none"}`}>
              <DropGlyph />
              {d.pop}%
            </div>
            <div className="tv-wx-drange">
              <span className="tv-wx-dlo">{d.lo_f}°</span>
              <span className="tv-wx-dtrack">
                <span className="tv-wx-dfill" style={{ left: 0, width: `${width}%` }} />
              </span>
              <span className="tv-wx-dhi">{d.hi_f}°</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function WeatherCard({ data }: ViewProps): ReactNode {
  const place = String(data.place ?? "");
  const asOf = typeof data.as_of === "string" ? data.as_of : "";
  const tz = typeof data.tz === "string" ? data.tz : "";
  const now = (data.now ?? {}) as Record<string, unknown>;
  const cond = wxCond(now.cond);
  const day = now.is_day !== false;
  const label = typeof now.label === "string" ? now.label : "";
  const hi = wxNum(data.hi_f);
  const lo = wxNum(data.lo_f);
  const wind = wxNum(now.wind_mph);
  const windDir = typeof now.wind_dir === "string" ? now.wind_dir : "";
  const hours: WxHour[] = (Array.isArray(data.hours) ? data.hours : []).map((h, i) => {
    const row = (h ?? {}) as Record<string, unknown>;
    return {
      label: i === 0 ? "Now" : String(row.label ?? ""),
      temp_f: wxNum(row.temp_f),
      cond: wxCond(row.cond),
      is_day: row.is_day !== false,
      pop: wxNum(row.pop),
    };
  });
  const days: WxDay[] = (Array.isArray(data.days) ? data.days : []).map((d) => {
    const row = (d ?? {}) as Record<string, unknown>;
    return {
      label: String(row.label ?? ""),
      cond: wxCond(row.cond),
      hi_f: wxNum(row.hi_f),
      lo_f: wxNum(row.lo_f),
      pop: wxNum(row.pop),
    };
  });
  const week = data.range === "week" && days.length > 0;
  const when = [asOf, tz].filter(Boolean).join(" ");

  return (
    <div className="tv-wx">
      <div className="tv-wx-cap">
        weather{place ? ` · ${place}` : ""}
        {week ? " · 7-day" : ""}
      </div>
      <div className="tv-wx-hero">
        <div className="tv-wx-glyph">
          <WeatherGlyph cond={cond} day={day} />
        </div>
        <div className="tv-wx-main">
          {when && <div className="tv-wx-when">{when}</div>}
          <div className="tv-wx-temp">
            {wxNum(now.temp_f)}
            <span className="tv-wx-deg">°F</span>
          </div>
          <div className="tv-wx-sub">
            {label}
            {label && " · "}
            <span className="tv-wx-feels">feels {wxNum(now.feels_f)}°</span>
          </div>
        </div>
        <div className="tv-wx-hilo">
          <b>H {hi}°</b>
          <br />L {lo}°
          {wind > 0 && (
            <>
              <br />
              <span className="tv-wx-wind">
                {windDir} {wind} mph
              </span>
            </>
          )}
        </div>
      </div>
      {week ? (
        <DailyList days={days} />
      ) : (
        hours.length > 0 && (
          <div className="tv-wx-strip">
            {hours.map((h, i) => (
              // Positional forecast rows have no stable id; the hour label + index key it.
              <div className={`tv-wx-hr${i === 0 ? " now" : ""}`} key={`${h.label}-${i}`}>
                <div className="tv-wx-ht">{h.label}</div>
                <WeatherGlyph cond={h.cond} day={h.is_day} />
                <div className="tv-wx-htemp">{h.temp_f}°</div>
                <div className={`tv-wx-pop${h.pop > 0 ? "" : " none"}`}>
                  <DropGlyph />
                  {h.pop}%
                </div>
              </div>
            ))}
          </div>
        )
      )}
    </div>
  );
}

/** The sub-agent fan result (docs/archive/SUBAGENT_SPAWNING_PLAN.md, Wave S3.2): a neutral
 * roster card — a ran/failed roll-up plus one line per child (label, neutral persona
 * tag, ✓/✕, summary). Data: `{ran, failed, children: [{label, persona, ok, summary}]}`.
 * Standard tool-view frame, never a bespoke green panel; colour stays on the marks
 * (green=ok, rose=failed). This is the persisted (reopened-transcript) stand-in for the
 * live in-chat accordion; like it, each child's summary is COLLAPSED behind its row
 * (tap to expand) so the card never dumps every full comment, and a failed child opens
 * itself so the error is visible without a tap. */
// The "open in its own session" glyph on each synthesis row — an arrow leaving a frame,
// the conventional "open elsewhere" mark. Neutral stroke; the theme owns the color.
function OpenSessionIcon(): ReactNode {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M14 4h6v6" />
      <path d="M20 4l-8 8" />
      <path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" />
    </svg>
  );
}

function SubagentSynthesis({ data, onOpenSession }: ViewProps): ReactNode {
  const ran = typeof data.ran === "number" ? data.ran : 0;
  const failed = typeof data.failed === "number" ? data.failed : 0;
  const skipped = typeof data.skipped === "number" ? data.skipped : 0;
  const truncated = data.truncated === true;
  const rawChildren = Array.isArray(data.children) ? data.children : [];
  const clean = failed === 0 && skipped === 0 && !truncated;
  const [open, setOpen] = useState<Set<number>>(new Set());
  function toggle(i: number): void {
    setOpen((cur) => {
      const next = new Set(cur);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  }
  const rows = rawChildren.map((raw, i) => {
    const c = raw as Record<string, unknown>;
    return {
      i,
      ok: c.ok === true,
      label: String(c.label ?? ""),
      persona: String(c.persona ?? ""),
      summary: typeof c.summary === "string" ? c.summary : "",
      sessionId: typeof c.session_id === "string" ? c.session_id : "",
      skipped: c.skipped === true,
      skipReason: typeof c.skip_reason === "string" ? c.skip_reason : "",
      wave: typeof c.wave === "number" ? c.wave : 0,
      fedFrom: Array.isArray(c.fed_from)
        ? c.fed_from.filter((x): x is string => typeof x === "string")
        : [],
    };
  });
  // A staged (feeding-waves) fan groups its roster by wave and shows feed edges; a
  // flat fan (no wave/feed data) renders as a single ungrouped list, unchanged.
  const staged = rows.some((r) => r.wave > 0 || r.fedFrom.length > 0);
  const maxWave = rows.reduce((m, r) => Math.max(m, r.wave), 0);

  const renderRow = (r: (typeof rows)[number]): ReactNode => {
    const mark = r.skipped ? "⊘" : r.ok ? "✓" : "✕";
    const markCls = r.skipped ? " skip" : r.ok ? "" : " bad";
    // A failure auto-expands its error; a skip shows its reason inline; a success stays
    // collapsed behind its row.
    const isOpen = open.has(r.i) || (!r.ok && !r.skipped);
    return (
      <div className="tv-syn-child" key={r.i}>
        {/* The toggle and the open-session link are SIBLING buttons in a flex row
            (never a button-in-button), so each is its own tap target. */}
        <div className="tv-syn-rowwrap">
          <button
            type="button"
            className="tv-syn-row"
            onClick={() => toggle(r.i)}
            aria-expanded={isOpen}
          >
            <span className={`tv-syn-mark${markCls}`} aria-hidden="true">
              {mark}
            </span>
            <span className="tv-syn-clbl">{r.label}</span>
            <span className="tv-syn-ptag">{r.persona}</span>
            {r.summary && !r.skipped && (
              <span className="tv-syn-car" aria-hidden="true">
                {isOpen ? "▾" : "▸"}
              </span>
            )}
          </button>
          {/* Deep-link this row to the sub-agent's own session (its full transcript),
              only when both a handler and a session id are present. A skip has none. */}
          {onOpenSession && r.sessionId && (
            <button
              type="button"
              className="tv-syn-open"
              title="Open sub-agent session"
              aria-label={`Open ${r.label || "sub-agent"} session`}
              onClick={() => onOpenSession(r.sessionId)}
            >
              <OpenSessionIcon />
            </button>
          )}
        </div>
        {/* The feed edge, as text (never a drawn cross-group connector) — Direction 1. */}
        {r.fedFrom.length > 0 && <div className="tv-syn-fed">← fed by {r.fedFrom.join(", ")}</div>}
        {r.skipped ? (
          <div className="tv-syn-skip">skipped — {r.skipReason}</div>
        ) : (
          isOpen && r.summary && <div className="tv-syn-sum">{r.summary}</div>
        )}
      </div>
    );
  };

  return (
    <div className={`tv-syn${clean ? "" : " has-fail"}`}>
      <div className="tv-syn-head">
        <span className={`tv-syn-ic${clean ? "" : " bad"}`} aria-hidden="true">
          {clean ? "✓" : "✕"}
        </span>
        <span>
          {truncated ? (
            // A child can be cut off by its step cap, wall-clock, or token budget — the
            // exact reason rides in each child's own summary, so the header stays generic
            // rather than claiming one cause (it used to always say "budget").
            "Partial synthesis — research truncated"
          ) : (
            <>
              Synthesized from {ran - failed} of {ran}
              {failed > 0 ? ` · ${failed} failed` : ""}
              {skipped > 0 ? ` · ${skipped} skipped` : ""}
            </>
          )}
        </span>
      </div>
      {staged
        ? Array.from({ length: maxWave + 1 }, (_unused, w) => {
            const wrows = rows.filter((r) => r.wave === w);
            if (wrows.length === 0) return null;
            const personas = [...new Set(wrows.map((r) => r.persona))].join(", ");
            return (
              // biome-ignore lint/suspicious/noArrayIndexKey: waves render in stable order
              <div className="tv-syn-wave" key={w}>
                <div className="tv-syn-wh">
                  Wave {w + 1} · {personas}
                  {w > 0 ? ` — fed by wave ${w}` : ""}
                </div>
                {wrows.map(renderRow)}
              </div>
            );
          })
        : rows.map(renderRow)}
    </div>
  );
}

/** The deep_research run's report (docs/plans/DEEP_RESEARCH_TOOL_PLAN.md, Wave D3): the
 * synthesized report Markdown, a provenance strip (complexity, source count, rounds, and
 * the revised / coverage-limited / truncated flags), and a collapsible sub-agent roster
 * whose rows deep-link to each child's own session on reopen. Data-only: the report
 * Markdown came from the synthesizer over the escaped-envelope findings (never
 * model-authored markup — it renders through <Markdown>, the same path as an assistant
 * turn), and every count is derived from DB-run state. The `[^n]` footnotes render as the
 * report's own numbered chips (its `## Sources` section maps them); the flags are enum
 * tones the theme colors, never a model-sent color (DESIGN.md). */
function DeepResearchReport({ data, onOpenSession }: ViewProps): ReactNode {
  const question = typeof data.question === "string" ? data.question : "";
  const complexity = typeof data.complexity === "string" ? data.complexity : "";
  const reportMd = typeof data.report_md === "string" ? data.report_md : "";
  const subAgents = typeof data.sub_agents === "number" ? data.sub_agents : 0;
  const rounds = typeof data.rounds === "number" ? data.rounds : 1;
  const revised = data.revised === true;
  const coverageLimited = data.coverage_limited === true;
  const truncated = data.truncated === true;
  const children = Array.isArray(data.children) ? data.children : [];
  const [openRoster, setOpenRoster] = useState(false);

  const chips = [
    complexity,
    `${subAgents} source${subAgents === 1 ? "" : "s"}`,
    `${rounds} round${rounds === 1 ? "" : "s"}`,
    revised ? "revised" : "",
  ].filter(Boolean);

  return (
    <div className="tv-dr">
      <div className="tv-dr-head">
        <span className="tv-dr-cap">deep research</span>
        {question && <span className="tv-dr-q">{question}</span>}
      </div>
      <div className="tv-dr-chips">
        {chips.map((c) => (
          <span className="tv-dr-chip" key={c}>
            {c}
          </span>
        ))}
        {coverageLimited && <span className="tv-dr-chip warn">coverage limited</span>}
        {truncated && <span className="tv-dr-chip warn">truncated</span>}
      </div>
      <div className="tv-dr-report">
        <Markdown text={reportMd} />
      </div>
      {children.length > 0 && (
        <div className="tv-dr-roster">
          <button
            type="button"
            className="tv-dr-rtoggle"
            aria-expanded={openRoster}
            onClick={() => setOpenRoster((v) => !v)}
          >
            <span aria-hidden="true">{openRoster ? "▾" : "▸"}</span> {children.length} sub-agent
            {children.length === 1 ? "" : "s"}
          </button>
          {openRoster && (
            <div className="tv-dr-rlist">
              {children.map((raw, i) => {
                const c = (raw ?? {}) as Record<string, unknown>;
                const label = String(c.label ?? "");
                const persona = String(c.persona ?? "");
                const ok = c.ok === true;
                const sessionId = typeof c.session_id === "string" ? c.session_id : "";
                return (
                  <div className="tv-syn-rowwrap tv-dr-row" key={sessionId || i}>
                    <span className="tv-syn-row">
                      <span className={`tv-syn-mark${ok ? "" : " bad"}`} aria-hidden="true">
                        {ok ? "✓" : "✕"}
                      </span>
                      <span className="tv-syn-clbl">{label}</span>
                      <span className="tv-syn-ptag">{persona}</span>
                    </span>
                    {onOpenSession && sessionId && (
                      <button
                        type="button"
                        className="tv-syn-open"
                        title="Open sub-agent session"
                        aria-label={`Open ${label || "sub-agent"} session`}
                        onClick={() => onOpenSession(sessionId)}
                      >
                        <OpenSessionIcon />
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// --- hurricane_card --------------------------------------------------------
// jerv's tabbed active-tropical-cyclone view (docs/reference/DESIGN.md "hurricane_card
// tool-view"; build plan docs/archive/HURRICANE_TABS_PLAN.md). Data-only slots;
// `kind`/`cat`/`proximity`/`alert.level`/`level` are closed enums the component maps to
// a glyph + tone — the model never sends a glyph, icon, or color. The `alert` slot is
// the ONLY watch/warning surface (NWS-sourced); a real warning is the one case the
// danger/rose banner shows. The Track tab draws the storm on real map tiles (the on-box
// /api/tiles proxy) from `{lat, lon}` slots — the public NHC track + cone plus the
// city-centre `you` pin (the scoped #9 relaxation, see backend hurricanetools.py). The
// only URL is `nhc_url`, the storm's public NHC graphics page. Upstream text (alert
// headline) renders as escaped text content only.

type HuKind =
  | "hurricane"
  | "typhoon"
  | "tropical-storm"
  | "tropical-depression"
  | "subtropical-storm"
  | "subtropical-depression"
  | "post-tropical"
  | "potential"
  | "low"
  | "cyclone";
const HU_KIND_LABEL: Record<HuKind, string> = {
  hurricane: "Hurricane",
  typhoon: "Typhoon",
  "tropical-storm": "Tropical Storm",
  "tropical-depression": "Tropical Depression",
  "subtropical-storm": "Subtropical Storm",
  "subtropical-depression": "Subtropical Depression",
  "post-tropical": "Post-Tropical",
  potential: "Potential Cyclone",
  low: "Tropical Low",
  cyclone: "Cyclone",
};
function huKind(value: unknown): HuKind {
  return typeof value === "string" && value in HU_KIND_LABEL ? (value as HuKind) : "cyclone";
}
type HuLevel = "low" | "moderate" | "high" | "extreme";
function huLevel(value: unknown): HuLevel {
  return value === "moderate" || value === "high" || value === "extreme" ? value : "low";
}

interface HuTimelineCell {
  label: string;
  wind_mph: number;
  gust_mph: number;
  rain_in: number;
  peak: boolean;
}
function huPoint(value: unknown): HuGeoPoint | null {
  const o = (value ?? {}) as Record<string, unknown>;
  const lat = Number(o.lat);
  const lon = Number(o.lon);
  return Number.isFinite(lat) && Number.isFinite(lon) ? { lat, lon } : null;
}

/** The cyclone spiral, drawn inline (no fetched icons, #9). */
function HurricaneGlyph(): ReactNode {
  return (
    <svg viewBox="0 0 24 24" className="tv-hu-svg" aria-hidden="true">
      <path d="M12 2a10 10 0 0 1 8 4c-3 0-5 1-6 3M12 22a10 10 0 0 1-8-4c3 0 5-1 6-3M22 12a10 10 0 0 1-4 8c0-3-1-5-3-6M2 12a10 10 0 0 1 4-8c0 3 1 5 3 6" />
      <circle cx="12" cy="12" r="2.3" />
    </svg>
  );
}

/** The official NWS alert banner — the only watch/warning surface. A `warning` reads
 * the rose danger tone; a `watch` reads amber. The event + headline are NWS strings
 * rendered as escaped text content (never markup, #9). */
function HuAlertBanner({ alert }: { alert: Record<string, unknown> }): ReactNode {
  const level = alert.level === "warning" ? "warning" : "watch";
  const event = String(alert.event ?? "");
  const headline = String(alert.headline ?? "");
  return (
    <div className={`tv-hu-alert ${level}`}>
      <svg viewBox="0 0 24 24" className="tv-hu-alert-svg" aria-hidden="true">
        <path d="M10.3 3.3 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.3a2 2 0 0 0-3.4 0z" />
        <path d="M12 9v4M12 17h.01" />
      </svg>
      <div>
        <b>{event}</b>
        {headline && <span className="tv-hu-alert-head"> — {headline}</span>}
      </div>
    </div>
  );
}

/** The Track tab: the storm on real map tiles (the on-box /api/tiles proxy) — the cone
 * polygon, the forecast path, its points (toned by category), and the city-centre place
 * pin — pannable/zoomable via Leaflet. The map geometry arrives as `{lat, lon}` (the
 * scoped #9 relaxation); Leaflet frames it. */
function HuTrackMap({
  track,
  cone,
  you,
}: { track: HuTrackPointGeo[]; cone: HuGeoPoint[]; you: HuGeoPoint | null }): ReactNode {
  const hasPast = track.some((p) => p.past);
  const mapRef = useRef<HTMLDivElement>(null);
  // The payload is immutable for the card's life; memoize so the effect redraws only
  // when the actual geometry identity changes, not on every parent render.
  const mapData = useMemo<HuMapData>(() => ({ track, cone, you }), [track, cone, you]);
  useEffect(() => {
    if (!mapRef.current) return;
    const handle = renderHurricaneMap(mapRef.current, mapData);
    // Leaflet mis-measures inside a just-shown tab (the pane was display:none until the
    // Track tab was selected); re-measure on the next frame so tiles fill the box.
    const t = setTimeout(() => handle.invalidate(), 60);
    return () => {
      clearTimeout(t);
      handle.destroy();
    };
  }, [mapData]);
  return (
    <div className="tv-hu-track">
      <div className="tv-hu-map" ref={mapRef} />
      <div className="tv-hu-track-legend">
        <span>
          <i className="tv-hu-dot fc" /> forecast
        </span>
        {hasPast && (
          <span>
            <i className="tv-hu-dot past" /> past
          </span>
        )}
        <span>
          <i className="tv-hu-dot cone" /> cone
        </span>
        {you && (
          <span>
            <i className="tv-hu-dot you" /> you
          </span>
        )}
      </div>
      {/* OSM/CARTO credit, carried here as a compact static line since the map's own
          Leaflet attribution banner is off (it overlapped the storm on a phone card). */}
      <div className="tv-hu-attr">© OpenStreetMap contributors · © CARTO</div>
    </div>
  );
}

/** The Timeline tab: a finger-scrollable strip of wind/gust/rain cells (peak flagged),
 * plus the derived (approximate) tropical-storm/hurricane-force arrival labels. */
function HuTimeline({
  cells,
  arrival,
}: { cells: HuTimelineCell[]; arrival: Record<string, unknown> }): ReactNode {
  const ts = typeof arrival.ts_force === "string" ? arrival.ts_force : "";
  const hu = typeof arrival.hurricane_force === "string" ? arrival.hurricane_force : "";
  return (
    <div className="tv-hu-timeline">
      <div className="tv-hu-strip">
        {cells.map((c, i) => (
          // Positional cells have no stable id; label + index key them.
          <div className={`tv-hu-cell${c.peak ? " peak" : ""}`} key={`${c.label}-${i}`}>
            <div className="tv-hu-ct">{c.label}</div>
            <div className="tv-hu-cg">
              {c.gust_mph}
              <span> mph</span>
            </div>
            <div className="tv-hu-cw">{c.wind_mph} sust</div>
            <div className={`tv-hu-cr${c.rain_in > 0 ? "" : " none"}`}>{c.rain_in}″</div>
          </div>
        ))}
      </div>
      {(ts || hu) && (
        <div className="tv-hu-arrival">
          {ts && (
            <span>
              TS-force <b>{ts}</b>
            </span>
          )}
          {hu && (
            <span>
              Hurricane-force <b>{hu}</b>
            </span>
          )}
          <span className="tv-hu-approx">approx.</span>
        </div>
      )}
    </div>
  );
}

/** One Impact-grid cell with a value and a severity gauge toned by `level`. A cell
 * whose quantity has no hazard magnitude (e.g. movement) passes `gauge={false}` and
 * reads the neutral `info` tone instead of a meaningless bar. */
function HuImpactCell({
  label,
  value,
  sub,
  level,
  fill,
  gauge = true,
}: {
  label: string;
  value: string;
  sub: string;
  level: HuLevel | "info";
  fill: number;
  gauge?: boolean;
}): ReactNode {
  return (
    <div className={`tv-hu-icell lv-${level}`}>
      <div className="tv-hu-ik">{label}</div>
      <div className="tv-hu-iv">{value}</div>
      {sub && <div className="tv-hu-iq">{sub}</div>}
      {gauge && (
        <div className="tv-hu-gauge">
          <i style={{ width: `${Math.max(0, Math.min(100, fill))}%` }} />
        </div>
      )}
    </div>
  );
}

const HU_FILL: Record<HuLevel, number> = { low: 30, moderate: 55, high: 78, extreme: 95 };

/** The Impact tab: a 2×2 hazard grid (wind/surge/rain/timing) with a My-impact ⇄
 * Storm-stats toggle. "My impact" is the NWS-derived local forecast; "Storm stats" is
 * the storm's own vitals. */
function HuImpact({
  impact,
  storm,
}: { impact: Record<string, unknown>; storm: Record<string, unknown> }): ReactNode {
  const [view, setView] = useState<"impact" | "storm">("impact");
  const wind = (impact.wind ?? null) as Record<string, unknown> | null;
  const surge = (impact.surge ?? null) as Record<string, unknown> | null;
  const rain = (impact.rain ?? null) as Record<string, unknown> | null;
  const timing = (impact.timing ?? null) as Record<string, unknown> | null;

  const myImpact: ReactNode[] = [];
  if (wind) {
    const lv = huLevel(wind.level);
    myImpact.push(
      <HuImpactCell
        key="wind"
        label="Wind"
        value={`${wxNum(wind.mph)} mph`}
        sub={`gusts ${wxNum(wind.gust)}`}
        level={lv}
        fill={HU_FILL[lv]}
      />,
    );
  }
  if (surge) {
    const lv = huLevel(surge.level);
    myImpact.push(
      <HuImpactCell
        key="surge"
        label="Surge"
        value={String(surge.band ?? "")}
        sub="above ground"
        level={lv}
        fill={HU_FILL[lv]}
      />,
    );
  }
  if (rain) {
    const lv = huLevel(rain.level);
    myImpact.push(
      <HuImpactCell
        key="rain"
        label="Rain"
        value={`${wxNum(rain.in)}″`}
        sub="storm total"
        level={lv}
        fill={HU_FILL[lv]}
      />,
    );
  }
  if (timing) {
    const onset = typeof timing.onset === "string" ? timing.onset : "—";
    const peak = typeof timing.peak === "string" ? timing.peak : "—";
    const clear = typeof timing.clear === "string" ? timing.clear : "—";
    myImpact.push(
      <div className="tv-hu-icell lv-info tv-hu-timing" key="timing">
        <div className="tv-hu-ik">Timing</div>
        <div className="tv-hu-tsteps">
          <span>
            Onset<b>{onset}</b>
          </span>
          <span>
            Peak<b>{peak}</b>
          </span>
          <span>
            Eases<b>{clear}</b>
          </span>
        </div>
      </div>,
    );
  }

  // Storm-stats gauges are toned + filled from the backend-computed severity tiers
  // (sustained_level/gust_level/pressure_level), so the bar tracks the real storm;
  // movement is a heading, not a hazard magnitude, so it shows no gauge.
  const cat = typeof storm.cat === "string" ? storm.cat : "";
  const sLv = huLevel(storm.sustained_level);
  const gLv = huLevel(storm.gust_level);
  const pLv = huLevel(storm.pressure_level);
  const gust = wxNum(storm.gust_mph);
  const stormStats: ReactNode[] = [
    <HuImpactCell
      key="sus"
      label="Sustained"
      value={`${wxNum(storm.sustained_mph)} mph`}
      sub={cat ? `Category ${cat}` : HU_KIND_LABEL[huKind(storm.kind)]}
      level={sLv}
      fill={HU_FILL[sLv]}
    />,
    <HuImpactCell
      key="gust"
      label="Peak gust"
      value={gust > 0 ? `${gust} mph` : "—"}
      sub={gust > 0 ? "near the core" : "no forecast"}
      level={gust > 0 ? gLv : "low"}
      fill={gust > 0 ? HU_FILL[gLv] : 0}
    />,
    <HuImpactCell
      key="pres"
      label="Pressure"
      value={`${wxNum(storm.pressure_mb)} mb`}
      sub="central"
      level={pLv}
      fill={HU_FILL[pLv]}
    />,
    <HuImpactCell
      key="move"
      label="Movement"
      value={String(storm.moving ?? "—")}
      sub=""
      level="info"
      fill={0}
      gauge={false}
    />,
  ];

  return (
    <div className="tv-hu-impact">
      <div className="tv-hu-seg">
        <button
          type="button"
          className={view === "impact" ? "on" : ""}
          onClick={() => setView("impact")}
        >
          My impact
        </button>
        <button
          type="button"
          className={view === "storm" ? "on" : ""}
          onClick={() => setView("storm")}
        >
          Storm stats
        </button>
      </div>
      <div className="tv-hu-grid">{view === "impact" ? myImpact : stormStats}</div>
    </div>
  );
}

type HuTab = "timeline" | "track" | "impact";

function HurricaneCard({ data }: ViewProps): ReactNode {
  const place = String(data.place ?? "");
  const asOf = typeof data.as_of === "string" ? data.as_of : "";
  const activeCount = wxNum(data.active_count);
  const storm = (data.storm ?? {}) as Record<string, unknown>;
  const name = String(storm.name ?? "");
  const kind = huKind(storm.kind);
  const cat = typeof storm.cat === "string" ? storm.cat : "";
  const sustained = wxNum(storm.sustained_mph);
  const gust = wxNum(storm.gust_mph);
  const pressure = wxNum(storm.pressure_mb);
  const moving = typeof storm.moving === "string" ? storm.moving : "";
  const distance = wxNum(data.distance_mi);
  const bearing = typeof data.bearing === "string" ? data.bearing : "";
  const alert = data.alert ? (data.alert as Record<string, unknown>) : null;
  const nhcUrl = typeof data.nhc_url === "string" ? data.nhc_url : "";
  const track: HuTrackPointGeo[] = (Array.isArray(data.track) ? data.track : []).flatMap((p) => {
    const pt = huPoint(p);
    if (!pt) return [];
    const row = (p ?? {}) as Record<string, unknown>;
    return [
      {
        ...pt,
        label: String(row.label ?? ""),
        cat: String(row.cat ?? ""),
        past: row.past === true,
      },
    ];
  });
  const cone: HuGeoPoint[] = (Array.isArray(data.cone) ? data.cone : []).flatMap((p) => {
    const pt = huPoint(p);
    return pt ? [pt] : [];
  });
  const you = huPoint(data.you);
  const timeline: HuTimelineCell[] = (Array.isArray(data.timeline) ? data.timeline : []).map(
    (c) => {
      const row = (c ?? {}) as Record<string, unknown>;
      return {
        label: String(row.label ?? ""),
        wind_mph: wxNum(row.wind_mph),
        gust_mph: wxNum(row.gust_mph),
        rain_in: Number.isFinite(Number(row.rain_in)) ? Number(row.rain_in) : 0,
        peak: row.peak === true,
      };
    },
  );
  const arrival = (data.arrival ?? {}) as Record<string, unknown>;
  const impact = (data.impact ?? {}) as Record<string, unknown>;

  const badge = cat ? `Cat ${cat}` : HU_KIND_LABEL[kind];
  const where = [distance > 0 ? `${distance} mi${bearing ? ` ${bearing}` : ""}` : "", moving]
    .filter(Boolean)
    .join(" · ");

  // Only offer a tab when its data is present (a `global`/out-of-coverage storm shows
  // the hero + Track only). Default to the most actionable available tab.
  const tabs: HuTab[] = [];
  if (timeline.length > 0) tabs.push("timeline");
  if (track.length > 0) tabs.push("track");
  // Offer Impact only when a slot actually carries content (the tab's render predicate),
  // so a keyed-but-empty `impact` never opens an empty grid.
  if (impact.wind || impact.surge || impact.rain || impact.timing) tabs.push("impact");
  const [tab, setTab] = useState<HuTab>(() => tabs[0] ?? "track");
  const active = tabs.includes(tab) ? tab : (tabs[0] ?? "track");

  return (
    <div className="tv-hu">
      <div className="tv-hu-cap">
        hurricane{place ? ` · ${place}` : ""}
        {activeCount > 1 ? ` · ${activeCount} active` : ""}
      </div>
      {alert && <HuAlertBanner alert={alert} />}
      <div className="tv-hu-hero">
        <div className="tv-hu-glyph">
          <HurricaneGlyph />
        </div>
        <div className="tv-hu-main">
          <div className="tv-hu-name">
            {name}
            <span className="tv-hu-badge">{badge}</span>
          </div>
          {where && <div className="tv-hu-where">{where}</div>}
          {asOf && <div className="tv-hu-when">as of {asOf}</div>}
        </div>
        <div className="tv-hu-vitals">
          {sustained > 0 && (
            <>
              <b>{sustained}</b> mph
            </>
          )}
          {gust > 0 && <div className="tv-hu-pres">gusts {gust}</div>}
          {pressure > 0 && <div className="tv-hu-pres">{pressure} mb</div>}
        </div>
      </div>

      {tabs.length > 0 && (
        <>
          <div className="tv-hu-tabs">
            {tabs.map((t) => (
              <button
                type="button"
                key={t}
                className={active === t ? "on" : ""}
                onClick={() => setTab(t)}
              >
                {t === "timeline" ? "Timeline" : t === "track" ? "Track" : "Impact"}
              </button>
            ))}
          </div>
          {active === "timeline" && <HuTimeline cells={timeline} arrival={arrival} />}
          {active === "track" && <HuTrackMap track={track} cone={cone} you={you} />}
          {active === "impact" && <HuImpact impact={impact} storm={storm} />}
        </>
      )}

      <div className="tv-hu-foot">
        {alert
          ? "Official NWS alert shown. Surge is a banded estimate and timing is approximate — follow official orders to evacuate."
          : "Storm position & forecast track. For watches, warnings & evacuation, check NWS/NHC and local emergency management."}
        {nhcUrl && (
          <a className="tv-hu-nhc" href={nhcUrl} target="_blank" rel="noopener noreferrer">
            National Hurricane Center forecast ↗
          </a>
        )}
      </div>
    </div>
  );
}

// --- chart & lab_chart -----------------------------------------------------
// The interactive time-series card (DESIGN.md "chart & lab_chart tool-views",
// variant C — tabbed multi-view). Data-only slots (#1/#9): the model fills numbers
// and a closed `flag` enum; the component owns the palette (steel = general, rose =
// health) and the zoom/pan interaction (InteractiveChart). `chart` is the generic
// series; `lab_chart` adds a reference band + abnormal-flag toning + a Range tab.

const POINT_FLAGS = new Set<PointFlag>(["normal", "low", "high", "critical"]);
function pointFlag(value: unknown): PointFlag {
  return typeof value === "string" && POINT_FLAGS.has(value as PointFlag)
    ? (value as PointFlag)
    : "normal";
}

interface CardPoint extends ChartPoint {
  note?: string;
}

function parsePoints(value: unknown): CardPoint[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((p): CardPoint => {
      const o = (p ?? {}) as Record<string, unknown>;
      return {
        x: Number(o.x),
        y: Number(o.y),
        flag: pointFlag(o.flag),
        ...(typeof o.note === "string" ? { note: o.note } : {}),
      };
    })
    .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y))
    .sort((a, b) => a.x - b.x);
}

function fmtNum(v: number): string {
  return Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
}
function fmtLong(x: number): string {
  const d = new Date(x);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
function flagLabel(f: PointFlag): string {
  return f === "normal" ? "in range" : f;
}

type CardTab = "trend" | "table" | "range" | "stats";

/** Read the y-scale from the payload, defaulting to the data's own min/max with headroom. */
function readYScale(
  data: Record<string, unknown>,
  pts: CardPoint[],
): {
  min: number;
  max: number;
  ticks: number[];
} {
  const y = (data.y ?? {}) as Record<string, unknown>;
  const ys = pts.map((p) => p.y);
  const dMin = Math.min(...ys);
  const dMax = Math.max(...ys);
  // Pad so a flat (all-equal) series still spans a nonzero range — otherwise
  // min===max and the plot divides by zero (NaN coordinates, a blank chart).
  const pad = (dMax - dMin) * 0.1 || Math.abs(dMax) * 0.1 || 1;
  const min = Number.isFinite(Number(y.min)) ? Number(y.min) : Math.floor(dMin - pad);
  const maxRaw = Number.isFinite(Number(y.max)) ? Number(y.max) : Math.ceil(dMax + pad);
  // Guarantee a positive span even if a producer hands us y.min === y.max.
  const max = maxRaw > min ? maxRaw : min + 1;
  const ticks = Array.isArray(y.ticks) ? y.ticks.map(Number).filter((t) => Number.isFinite(t)) : [];
  return { min, max, ticks: ticks.length ? ticks : [min, (min + max) / 2, max].map(Math.round) };
}

function ReadoutLine({
  point,
  unit,
  band,
}: {
  point: CardPoint;
  unit: string;
  band: RefBandData | null;
}): ReactNode {
  return (
    <div className="tv-cc-readout">
      <span className="tv-cc-rd">{fmtLong(point.x)}</span>
      <span className="tv-cc-rv">
        <b>{fmtNum(point.y)}</b> {unit}
        {band ? ` · ref ${fmtNum(band.lo)}–${fmtNum(band.hi)}` : ""}
        {point.note ? (
          <>
            {" · "}
            <span className="tv-cc-cite">{point.note}</span>
          </>
        ) : null}
      </span>
      {band && (
        <span className={`tv-cc-rf fl-${point.flag ?? "normal"}`}>
          {flagLabel(point.flag ?? "normal")}
        </span>
      )}
    </div>
  );
}

interface RefBandData {
  lo: number;
  hi: number;
  label: string;
}
function readRefBand(data: Record<string, unknown>): RefBandData | null {
  const r = data.ref as Record<string, unknown> | undefined;
  if (!r) return null;
  const lo = Number(r.lo);
  const hi = Number(r.hi);
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
  return {
    lo,
    hi,
    label: typeof r.label === "string" ? r.label : `reference ${fmtNum(lo)}–${fmtNum(hi)}`,
  };
}

/** The Range tab (lab): each recent reading gauged against the reference band. */
function RangeView({
  pts,
  unit,
  band,
}: {
  pts: CardPoint[];
  unit: string;
  band: RefBandData;
}): ReactNode {
  const axMin = Math.min(band.lo * 0.8, Math.min(...pts.map((p) => p.y)));
  const axMax = Math.max(band.hi * 1.05, Math.max(...pts.map((p) => p.y)));
  const span = axMax - axMin || 1;
  const pos = (v: number) => `${((v - axMin) / span) * 100}%`;
  const recent = pts.slice(-6).reverse();
  return (
    <div className="tv-cc-range">
      <div className="tv-cc-range-axis">
        <span>{fmtNum(axMin)}</span>
        <span>{band.label}</span>
        <span>{fmtNum(axMax)}</span>
      </div>
      {recent.map((p, i) => (
        // Composite key: two draws could share a timestamp; x alone isn't unique.
        <div className="tv-cc-gauge-row" key={`${p.x}-${i}`}>
          <div className="tv-cc-gauge-head">
            <span className="tv-cc-gauge-date">{fmtLong(p.x)}</span>
            <span className="tv-cc-gauge-val">
              <b>{fmtNum(p.y)}</b> {unit}
            </span>
            <span className={`tv-cc-gauge-flag fl-${p.flag ?? "normal"}`}>
              {flagLabel(p.flag ?? "normal")}
            </span>
          </div>
          <div className="tv-cc-gauge-track">
            <span
              className="tv-cc-gauge-band"
              style={{ left: pos(band.lo), right: `calc(100% - ${pos(band.hi)})` }}
            />
            <span className={`tv-cc-gauge-mark ${p.flag ?? "normal"}`} style={{ left: pos(p.y) }} />
          </div>
        </div>
      ))}
    </div>
  );
}

/** The Stats tab (generic): current / change / min / max / average. */
function StatsView({ pts, unit }: { pts: CardPoint[]; unit: string }): ReactNode {
  const ys = pts.map((p) => p.y);
  const first = ys[0] ?? 0;
  const last = ys[ys.length - 1] ?? 0;
  const change = Math.round((last - first) * 100) / 100;
  const avg = Math.round((ys.reduce((a, b) => a + b, 0) / ys.length) * 100) / 100;
  const stats: { label: string; value: string }[] = [
    { label: "current", value: `${fmtNum(last)} ${unit}` },
    { label: "change", value: `${change > 0 ? "+" : ""}${fmtNum(change)} ${unit}` },
    { label: "min", value: `${fmtNum(Math.min(...ys))} ${unit}` },
    { label: "max", value: `${fmtNum(Math.max(...ys))} ${unit}` },
    { label: "average", value: `${fmtNum(avg)} ${unit}` },
    { label: "readings", value: String(pts.length) },
  ];
  return (
    <div className="tv-cc-stats">
      {stats.map((s) => (
        <div className="tv-cc-stat" key={s.label}>
          <div className="tv-cc-stat-v">{s.value}</div>
          <div className="tv-cc-stat-l">{s.label}</div>
        </div>
      ))}
    </div>
  );
}

function ChartTable({
  pts,
  unit,
  band,
}: { pts: CardPoint[]; unit: string; band: RefBandData | null }): ReactNode {
  return (
    <table className="tv-cc-tbl">
      <thead>
        <tr>
          <th>Date</th>
          <th>Value</th>
          {band && <th>Ref</th>}
          {band && <th>Flag</th>}
          <th>Source</th>
        </tr>
      </thead>
      <tbody>
        {pts
          .slice()
          .reverse()
          .map((p, i) => (
            // Composite key: two draws could share a timestamp; x alone isn't unique.
            <tr
              key={`${p.x}-${i}`}
              className={p.flag === "critical" ? "crit" : p.flag && p.flag !== "normal" ? "ab" : ""}
            >
              <td>{fmtLong(p.x)}</td>
              <td>
                <b>{fmtNum(p.y)}</b> {unit}
              </td>
              {band && <td>{`${fmtNum(band.lo)}–${fmtNum(band.hi)}`}</td>}
              {band && (
                <td>
                  <span className={`fl-${p.flag ?? "normal"}`}>
                    {p.flag && p.flag !== "normal" ? p.flag : "—"}
                  </span>
                </td>
              )}
              <td className="tv-cc-cite">{p.note ?? "—"}</td>
            </tr>
          ))}
      </tbody>
    </table>
  );
}

function ChartCard({ data }: ViewProps): ReactNode {
  const pts = useMemo(
    () => parsePoints((data.series as Record<string, unknown>[])?.[0]?.points),
    [data.series],
  );
  const ref = readRefBand(data);
  const domain = data.domain === "health" ? "health" : "general";
  const unit = typeof data.unit === "string" ? data.unit : "";
  const title = typeof data.title === "string" ? data.title : ref ? "Lab result" : "Chart";
  const yScale = useMemo(() => readYScale(data, pts), [data, pts]);
  const tabs: CardTab[] = ref ? ["trend", "table", "range"] : ["trend", "table", "stats"];
  const [tab, setTab] = useState<CardTab>("trend");
  const [sel, setSel] = useState<CardPoint | null>(() => pts.at(-1) ?? null);

  const last = pts.at(-1);
  const first = pts[0];
  if (!last || !first) {
    return <div className="tv-cc-empty">No data to plot.</div>;
  }

  const delta = Math.round((last.y - first.y) * 100) / 100;
  const dir = delta > 0 ? "up" : delta < 0 ? "down" : "flat";

  return (
    <div className={`tv-cc dom-${domain}`}>
      <div className="tv-cc-cap">
        {ref ? "lab · " : ""}
        {title.toLowerCase()} · {pts.length} points
      </div>
      <div className="tv-cc-head">
        <span className="tv-cc-now">{fmtNum(last.y)}</span>
        <span className="tv-cc-unit">{unit}</span>
        <span className={`tv-cc-delta ${dir}`}>
          {delta > 0 ? "▲" : delta < 0 ? "▼" : "—"} {fmtNum(Math.abs(delta))} {unit} since{" "}
          {fmtLong(first.x)}
        </span>
      </div>
      <div className="tv-cc-seg" role="tablist" aria-label={`${title} views`}>
        {tabs.map((t) => (
          <button
            type="button"
            key={t}
            role="tab"
            aria-selected={tab === t}
            className={tab === t ? "on" : ""}
            onClick={() => setTab(t)}
          >
            {t === "trend" ? "Trend" : t === "table" ? "Table" : t === "range" ? "Range" : "Stats"}
          </button>
        ))}
      </div>
      {tab === "trend" && (
        <div className="tv-cc-trend">
          {sel && <ReadoutLine point={sel} unit={unit} band={ref} />}
          <InteractiveChart
            // Remount on (re)entering Trend so pointer listeners bind once and the view resets.
            key="trend"
            points={pts}
            y={yScale}
            domain={domain}
            kind={data.kind === "area" ? "area" : "line"}
            label={`${title} over time`}
            onScrub={(p) => setSel(p as CardPoint)}
            {...(ref ? { refBand: ref } : {})}
          />
          <div className="tv-cc-hint">pinch or scroll to zoom · drag to pan · tap a point</div>
        </div>
      )}
      {tab === "table" && <ChartTable pts={pts} unit={unit} band={ref} />}
      {tab === "range" && ref && <RangeView pts={pts} unit={unit} band={ref} />}
      {tab === "stats" && <StatsView pts={pts} unit={unit} />}
    </div>
  );
}

const REGISTRY: Record<string, (props: ViewProps) => ReactNode> = {
  stat_block: StatBlock,
  data_table: DataTable,
  citation_card: CitationCard,
  list_card: ListCard,
  appointment_card: AppointmentCard,
  location_map: LocationMap,
  place_card: PlaceCard,
  generated_image: GeneratedImage,
  transcript: Transcript,
  video_analysis: VideoAnalysisView,
  task_status: TaskStatusView,
  server_metrics: ServerMetrics,
  weather_card: WeatherCard,
  hurricane_card: HurricaneCard,
  subagent_synthesis: SubagentSynthesis,
  deep_research_report: DeepResearchReport,
  chart: ChartCard,
  lab_chart: ChartCard,
};

export function isKnownView(name: string): boolean {
  return name in REGISTRY;
}

/** Render a tool-result view from its payload, or nothing if the named component
 * is not registered (an unknown `view` is rejected, never rendered). */
export function ToolView({
  payload,
  onOpenSession,
  onDeferredComplete,
}: {
  payload: ViewPayload;
  onOpenSession?: ((sessionId: string) => void) | undefined;
  onDeferredComplete?: ((resumeMessage: string) => void) | undefined;
}): ReactNode {
  const Component = REGISTRY[payload.view];
  if (!Component) return null;
  return (
    <div className={`tool-view surface-${payload.surface}`}>
      <Component
        data={payload.data}
        refs={payload.refs}
        onOpenSession={onOpenSession}
        onDeferredComplete={onDeferredComplete}
      />
    </div>
  );
}
