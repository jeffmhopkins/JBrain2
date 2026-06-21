// The tool-view component registry: a fixed map from a `view` name to a
// first-party React component, and <ToolView> which renders the named component
// from a ViewPayload — or NOTHING if the name is unknown. This is invariant #1/#9
// (DESIGN.md "Agent tool views"): model output never authors markup; it only
// selects a registered component and fills its data-only slots. Adding a
// component is a deliberate change here, like adding a tool.

import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { generatedImageSourceUrl, generatedImageUrl } from "../../api/client";
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

type GenView = "compare" | "before" | "after";
// The toggle pins the wipe to an edge (Before = show all "before" = 100%;
// After = 0%) or restores the midpoint for free dragging (Compare = 50%).
const VIEW_POS: Record<GenView, number> = { compare: 50, before: 100, after: 0 };

function EditCompare({
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
  const [view, setView] = useState<GenView>("compare");
  const dragging = useRef(false);

  function moveTo(clientX: number): void {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0) return;
    const next = Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));
    setPos(next);
    setView("compare");
  }

  function pick(next: GenView): void {
    setView(next);
    setPos(VIEW_POS[next]);
  }

  return (
    <div className="tv-genimg-edit">
      {/* The two images carry the accessible names (Before:/After:); the drag is
          a pointer affordance, with the keyboard equivalent in the toggle below. */}
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
        <span className="tv-genimg-kind kind-edit">edited</span>
        <span className="tv-genimg-lbl b">BEFORE</span>
        <span className="tv-genimg-lbl a">AFTER</span>
        <div className="tv-genimg-handle" aria-hidden="true" />
        <div className="tv-genimg-grip" aria-hidden="true" />
      </div>
      <div className="tv-genimg-acts">
        <span className="tv-genimg-cap">{alt}</span>
        {/* biome-ignore lint/a11y/useSemanticElements: a 3-button toggle group; a <fieldset> is overkill */}
        <div className="tv-genimg-seg" role="group" aria-label="Compare view">
          {(["compare", "before", "after"] as GenView[]).map((m) => (
            <button
              key={m}
              type="button"
              className={view === m ? "on" : ""}
              aria-pressed={view === m}
              onClick={() => pick(m)}
            >
              {m === "compare" ? "Compare" : m === "before" ? "Before" : "After"}
            </button>
          ))}
        </div>
      </div>
    </div>
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
      <div className="tv-genimg">
        <EditCompare
          beforeSrc={generatedImageSourceUrl(imageId)}
          afterSrc={generatedImageUrl(imageId)}
          width={width}
          height={height}
          alt={meta}
        />
      </div>
    );
  }

  return <GenerateImage src={generatedImageUrl(imageId)} alt={alt} meta={meta} data={data} />;
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
  const width = asDim(data.width);
  const height = asDim(data.height);
  // The last live preview frame, handed down from the in-flight turn (live only, absent
  // on reopen): held as the placeholder until the full-res image loads, so there's no
  // blank gap between "finalizing" and the rendered image.
  const placeholder =
    typeof data.placeholder_data_uri === "string" ? data.placeholder_data_uri : "";
  const [loaded, setLoaded] = useState(false);
  const [zoom, setZoom] = useState(false);

  return (
    <div className="tv-genimg">
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
      <div className="tv-genimg-cap">{meta}</div>
      {zoom && <Lightbox src={src} alt={alt} onClose={() => setZoom(false)} />}
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
