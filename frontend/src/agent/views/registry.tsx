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
import { TimeSeriesPlot } from "../../components/TimeSeriesPlot";
import { VideoAnalysis, type VideoFrame } from "../../components/VideoAnalysis";
import { serverMetricSeries } from "../../components/serverMetricSeries";
import type { CitationRef, ViewPayload } from "../types";
import { Lightbox } from "./Lightbox";
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

/** Map the tool-view frame shape ({t_ms, caption, thumb_id}) to the card's props,
 * building each frame's thumbnail src from its blob id via `thumbUrl` (the backend
 * validates the id against the attachment's stored frames under the firewall). A
 * frame whose thumbnail can't be addressed (no builder) renders without a still. */
function videoFrames(value: unknown, thumbUrl: ((thumbId: string) => string) | null): VideoFrame[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((f): VideoFrame[] => {
    if (typeof f !== "object" || f === null) return [];
    const o = f as Record<string, unknown>;
    if (typeof o.caption !== "string") return [];
    const thumb = thumbUrl && typeof o.thumb_id === "string" ? thumbUrl(o.thumb_id) : undefined;
    return [{ tMs: typeof o.t_ms === "number" ? o.t_ms : 0, caption: o.caption, thumbUrl: thumb }];
  });
}

/** `{attachment_id, source, filename, summary, duration_ms, frames:[{t_ms, caption,
 * thumb_id}], transcript:{text, words}|null}` — the analyze_video card. The component
 * builds the media + thumbnail srcs from the id + source (a chat attachment for jerv's
 * tool, a note attachment otherwise); no URL rides the payload (invariant #9). */
function VideoAnalysisView({ data }: ViewProps): ReactNode {
  const attachmentId = String(data.attachment_id ?? "");
  const source = data.source === "note" ? "note" : "chat";
  const videoUrl =
    source === "note" ? attachmentUrl(attachmentId) : chatAttachmentUrl(attachmentId);
  // Thumbnails are served only for chat attachments (a note-attachment thumbnail route
  // arrives with the note card); a note source renders frame markers without stills.
  const thumbUrl =
    source === "chat" ? (id: string) => chatAttachmentThumbUrl(attachmentId, id) : null;
  const transcript =
    data.transcript && typeof data.transcript === "object"
      ? (data.transcript as Record<string, unknown>)
      : null;
  return (
    <VideoAnalysis
      videoUrl={videoUrl}
      filename={typeof data.filename === "string" ? data.filename : "video"}
      summary={typeof data.summary === "string" ? data.summary : ""}
      frames={videoFrames(data.frames, thumbUrl)}
      words={transcriptWords(transcript?.words)}
      transcriptText={typeof transcript?.text === "string" ? transcript.text : undefined}
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
// jerv's in-chat forecast (docs/DESIGN.md "weather_card tool-view", variant A —
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
  server_metrics: ServerMetrics,
  weather_card: WeatherCard,
};

export function isKnownView(name: string): boolean {
  return name in REGISTRY;
}

/** Render a tool-result view from its payload, or nothing if the named component
 * is not registered (an unknown `view` is rejected, never rendered). */
export function ToolView({ payload }: { payload: ViewPayload }): ReactNode {
  const Component = REGISTRY[payload.view];
  if (!Component) return null;
  return (
    <div className={`tool-view surface-${payload.surface}`}>
      <Component data={payload.data} refs={payload.refs} />
    </div>
  );
}
