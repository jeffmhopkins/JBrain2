// A reusable, presentational time-series chart with X-axis zoom + pan — the
// numeric analogue of the Leaflet location views (DESIGN.md "chart & lab_chart
// tool-views"). Callers pass parsed points + a fixed Y scale + token classes; the
// component never reads model data directly, so it is safe to drive from an agent
// tool view. Interaction: pinch/wheel zoom anchored at the pointer, one-finger
// drag pan (clamped to the data bounds), tap/keyboard to scrub a point and pin a
// readout via `onScrub`. `touch-action: pan-y` keeps vertical page scroll working —
// a chart never traps the scroll. Honors prefers-reduced-motion (it animates nothing).

import { type ReactNode, useCallback, useId, useMemo, useRef, useState } from "react";

export type PointFlag = "normal" | "low" | "high" | "critical";

export interface ChartPoint {
  /** Epoch milliseconds (the X axis is time). */
  x: number;
  y: number;
  /** Lab abnormal flag; the component tones the point from it (never a model color). */
  flag?: PointFlag;
}

export interface RefBand {
  lo: number;
  hi: number;
  label: string;
}

export interface InteractiveChartProps {
  points: ChartPoint[];
  y: { min: number; max: number; ticks: number[] };
  /** "general" (steel) or "health" (rose) — tones the line + selection. */
  domain: "general" | "health";
  /** "line" (default) or "area" — an area fills under the line to the baseline. */
  kind?: "line" | "area";
  /** A reference band drawn as a tinted zone with a dashed lower edge (lab plots). */
  refBand?: RefBand;
  /** Called when the selection changes (tap / keyboard), for the parent's readout. */
  onScrub?: (point: ChartPoint, index: number) => void;
  /** Accessible name for the plot (e.g. "Platelet count over time"). */
  label: string;
}

const W = 352;
const H = 300;
const L = 38;
const R = 12;
const T = 14;
const B = 26;
const DAY = 86_400_000;

function fmtAxisDate(x: number): string {
  return new Date(x).toLocaleDateString(undefined, { month: "short", year: "2-digit" });
}

/** Nice ~4-6 date ticks across the visible span. */
function dateTicks(vs: number, ve: number): number[] {
  const days = (ve - vs) / DAY;
  const step = days < 70 ? 7 * DAY : days < 240 ? 30.4 * DAY : days < 560 ? 61 * DAY : 122 * DAY;
  const out: number[] = [];
  for (let t = Math.ceil(vs / step) * step; t <= ve && out.length < 7; t += step) out.push(t);
  return out;
}

export function InteractiveChart({
  points,
  y,
  domain,
  kind = "line",
  refBand,
  onScrub,
  label,
}: InteractiveChartProps): ReactNode {
  // Points are immutable for the chart's life; sort once and derive the full bounds.
  const pts = useMemo(() => points.slice().sort((a, b) => a.x - b.x), [points]);
  const bounds = useMemo(() => {
    const first = pts[0];
    const lastP = pts[pts.length - 1];
    if (!first || !lastP) return null;
    const pad = (lastP.x - first.x) * 0.04 || DAY;
    const start = first.x - pad;
    const end = lastP.x + pad;
    return { start, end, span: end - start, minSpan: (end - start) / 25 };
  }, [pts]);

  const [view, setView] = useState<{ vs: number; ve: number } | null>(null);
  const [sel, setSel] = useState(pts.length - 1);
  const svgRef = useRef<SVGSVGElement>(null);
  const clipId = useId();
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const panStart = useRef<{ x: number; vs: number; ve: number } | null>(null);
  const pinchStart = useRef<{ d: number; cx: number; vs: number; ve: number } | null>(null);
  const moved = useRef(false);

  // The visible domain: `view` once the reader has zoomed/panned, else the full span.
  const vs = view ? view.vs : (bounds?.start ?? 0);
  const ve = view ? view.ve : (bounds?.end ?? 1);
  const zoomed = !!view && !!bounds && ve - vs < bounds.span - 1;

  const setSelected = useCallback(
    (i: number) => {
      const p = pts[i];
      if (!p) return;
      setSel(i);
      onScrub?.(p, i);
    },
    [pts, onScrub],
  );

  if (!bounds || pts.length === 0) {
    return <div className="tv-plot-empty">No data to plot.</div>;
  }

  const px = (x: number) => L + ((x - vs) / (ve - vs)) * (W - L - R);
  const invX = (sx: number) => vs + ((sx - L) / (W - L - R)) * (ve - vs);
  const py = (v: number) => T + (1 - (v - y.min) / (y.max - y.min)) * (H - T - B);

  function clamp(nvs: number, nve: number): { vs: number; ve: number } {
    if (!bounds) return { vs: nvs, ve: nve };
    let s = nvs;
    let e = nve;
    let span = e - s;
    if (span < bounds.minSpan) {
      const m = (s + e) / 2;
      s = m - bounds.minSpan / 2;
      e = m + bounds.minSpan / 2;
      span = bounds.minSpan;
    }
    if (span >= bounds.span) return { vs: bounds.start, ve: bounds.end };
    if (s < bounds.start) {
      s = bounds.start;
      e = s + span;
    }
    if (e > bounds.end) {
      e = bounds.end;
      s = e - span;
    }
    return { vs: s, ve: e };
  }

  function localX(clientX: number): number {
    const r = svgRef.current?.getBoundingClientRect();
    if (!r || r.width === 0) return W / 2;
    return ((clientX - r.left) / r.width) * W;
  }

  function zoomAt(anchorX: number, factor: number): void {
    setView(clamp(anchorX - (anchorX - vs) * factor, anchorX + (ve - anchorX) * factor));
  }

  function nearest(sx: number): number {
    const xd = invX(sx);
    let best = 0;
    let bd = Number.POSITIVE_INFINITY;
    pts.forEach((p, i) => {
      const dd = Math.abs(p.x - xd);
      if (dd < bd) {
        bd = dd;
        best = i;
      }
    });
    return best;
  }

  function onWheel(e: React.WheelEvent): void {
    e.preventDefault();
    zoomAt(invX(localX(e.clientX)), e.deltaY > 0 ? 1.18 : 0.85);
  }

  function onPointerDown(e: React.PointerEvent): void {
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    moved.current = false;
    if (pointers.current.size === 1) {
      panStart.current = { x: e.clientX, vs, ve };
    } else if (pointers.current.size === 2) {
      const [a0, a1] = [...pointers.current.values()];
      if (!a0 || !a1) return;
      pinchStart.current = { d: Math.abs(a0.x - a1.x) || 1, cx: (a0.x + a1.x) / 2, vs, ve };
      panStart.current = null;
    }
  }

  function onPointerMove(e: React.PointerEvent): void {
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const r = svgRef.current?.getBoundingClientRect();
    if (!r || r.width === 0) return;
    if (pointers.current.size === 2 && pinchStart.current) {
      const [a0, a1] = [...pointers.current.values()];
      if (!a0 || !a1) return;
      const dist = Math.abs(a0.x - a1.x) || 1;
      const f = pinchStart.current.d / dist;
      const anchor =
        pinchStart.current.vs +
        ((((pinchStart.current.cx - r.left) / r.width) * W - L) / (W - L - R)) *
          (pinchStart.current.ve - pinchStart.current.vs);
      setView(
        clamp(
          anchor - (anchor - pinchStart.current.vs) * f,
          anchor + (pinchStart.current.ve - anchor) * f,
        ),
      );
      moved.current = true;
    } else if (pointers.current.size === 1 && panStart.current) {
      const dx = e.clientX - panStart.current.x;
      if (Math.abs(dx) > 3) moved.current = true;
      const domPerPx = (panStart.current.ve - panStart.current.vs) / ((r.width * (W - L - R)) / W);
      setView(clamp(panStart.current.vs - dx * domPerPx, panStart.current.ve - dx * domPerPx));
    }
  }

  function onPointerUp(e: React.PointerEvent): void {
    if (!pointers.current.has(e.pointerId)) return;
    if (pointers.current.size === 1 && !moved.current) setSelected(nearest(localX(e.clientX)));
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinchStart.current = null;
    if (pointers.current.size === 1) {
      // A pinch dropped to one finger: re-anchor pan on the survivor so dragging
      // keeps working without lifting and re-touching.
      const [only] = [...pointers.current.values()];
      if (only) panStart.current = { x: only.x, vs, ve };
      moved.current = true; // was a pinch; don't treat the next lift as a tap-select
    }
    if (pointers.current.size === 0) panStart.current = null;
  }

  function onKeyDown(e: React.KeyboardEvent): void {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      setSelected(Math.max(0, sel - 1));
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      setSelected(Math.min(pts.length - 1, sel + 1));
    }
  }

  // Line: include the points just outside the window so the stroke enters/exits cleanly.
  let iA = 0;
  let iB = pts.length - 1;
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i];
    if (!p) continue;
    if (p.x < vs) iA = i;
    if (p.x > ve) {
      iB = i;
      break;
    }
  }
  const vis = pts.slice(iA, iB + 1);
  const line = vis
    .map((p, i) => `${i ? "L" : "M"}${px(p.x).toFixed(1)} ${py(p.y).toFixed(1)}`)
    .join(" ");
  // Area fill (kind="area"): the line closed down to the baseline.
  const first = vis[0];
  const last = vis[vis.length - 1];
  const area =
    kind === "area" && first && last
      ? `M${px(first.x).toFixed(1)} ${H - B} ${line.replace(/^M/, "L")} L${px(last.x).toFixed(1)} ${H - B} Z`
      : "";

  const selPt = pts[sel];
  const selVisible = !!selPt && selPt.x >= vs - DAY && selPt.x <= ve + DAY;

  return (
    // The plot is a custom scrub/zoom widget: role="application" makes it a focusable,
    // keyboard-operable target (←/→ step the selection) that passes keys through to us.
    <div
      className="tv-plot-wrap"
      role="application"
      aria-label={label}
      // biome-ignore lint/a11y/noNoninteractiveTabindex: a custom keyboard-operable scrub/zoom widget (←/→ step the selection)
      tabIndex={0}
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onKeyDown={onKeyDown}
    >
      <svg
        ref={svgRef}
        className={`tv-plot dom-${domain}`}
        viewBox={`0 0 ${W} ${H}`}
        aria-hidden="true"
      >
        <clipPath id={clipId}>
          <rect x={L} y={T - 4} width={W - L - R} height={H - T - B + 4} />
        </clipPath>
        {/* Y grid + labels */}
        {y.ticks.map((v) => (
          <g key={v}>
            <line className="tv-plot-grid" x1={L} y1={py(v)} x2={W - R} y2={py(v)} />
            <text className="tv-plot-ax" x={L - 4} y={py(v) + 3} textAnchor="end">
              {v}
            </text>
          </g>
        ))}
        {/* Reference band (lab) */}
        {refBand && (
          <>
            <rect
              className="tv-plot-band"
              x={L}
              y={py(Math.min(refBand.hi, y.max))}
              width={W - L - R}
              height={py(Math.max(refBand.lo, y.min)) - py(Math.min(refBand.hi, y.max))}
            />
            <line
              className="tv-plot-band-edge"
              x1={L}
              y1={py(refBand.lo)}
              x2={W - R}
              y2={py(refBand.lo)}
            />
            <text className="tv-plot-band-lbl" x={W - R} y={py(refBand.lo) - 4} textAnchor="end">
              {refBand.label}
            </text>
          </>
        )}
        {/* Clipped line + points */}
        <g clipPath={`url(#${clipId})`}>
          {selVisible && selPt && (
            <line className="tv-plot-vrule" x1={px(selPt.x)} y1={T} x2={px(selPt.x)} y2={H - B} />
          )}
          {area && <path className="tv-plot-area" d={area} />}
          <path className="tv-plot-line" d={line} />
          {pts.map((p, i) =>
            p.x < vs - DAY || p.x > ve + DAY ? null : (
              <circle
                // biome-ignore lint/suspicious/noArrayIndexKey: points are a stable ordered series
                key={i}
                className={`tv-plot-pt ${p.flag ?? "normal"}${i === sel ? " sel" : ""}`}
                cx={px(p.x)}
                cy={py(p.y)}
                r={i === sel ? 5.5 : 4}
              />
            ),
          )}
        </g>
        {/* X date labels */}
        {dateTicks(vs, ve).map((t) => (
          <text key={t} className="tv-plot-ax" x={px(t)} y={H - 8} textAnchor="middle">
            {fmtAxisDate(t)}
          </text>
        ))}
      </svg>
      {zoomed && (
        <button type="button" className="tv-plot-reset" onClick={() => setView(null)}>
          reset zoom
        </button>
      )}
    </div>
  );
}
