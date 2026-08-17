// A reusable, presentational time-series plot: a stack of labeled sparklines.
// Each series renders one mini line chart (current-value readout + peak/low axis)
// in a 100×32 viewBox stretched to the container; a null sample breaks the line
// (a gap, not a drop to zero), and an all-null series is omitted. A series can
// carry more than one line (e.g. network down/up) — they share a Y-scale and the
// panel, with a colored legend. Callers own the palette and formatters — the
// component never reads model data directly, so it's safe to drive from both the
// Ops screen and an agent tool view.

import type { ReactNode } from "react";

export interface PlotLine {
  /** Legend sub-label when a plot carries more than one line (e.g. "down"). Omit
   * for a single-line plot, where the row label already names it. */
  label?: string;
  /** Stroke color — a CSS token the caller owns (never model-authored). */
  color: string;
  /** The line itself (typically the per-bucket average); null breaks the line. */
  values: (number | null)[];
  /** Optional per-bucket peak (bucket max), drawn as a fainter, thinner line ABOVE
   * the average — the gap between the two lines is the headroom — so a spike
   * shorter than a bucket isn't averaged out of view. Not a fill: an area anchored
   * to the floor reads as "quantity used", not "peak". */
  band?: (number | null)[];
  /** Shade the area between the line and the floor. Opt-in, because it only reads
   * honestly for a series whose floor IS zero — a load percentage. On an auto-scaled
   * series the baseline is the window's minimum, so the shading would start part-way
   * up and imply a quantity that isn't there. */
  fill?: boolean;
}

export interface PlotSeries {
  /** Row label, shown left of the current value(s). */
  label: string;
  /** One or more lines sharing this panel's Y-scale. */
  lines: PlotLine[];
  /** Formats the current / peak / low readouts. */
  fmt: (v: number) => string;
  /** Pins the drawing's Y-scale instead of fitting it to the data. For a BOUNDED unit
   *  — a percentage — fitting is actively misleading: a window that never left 0–3%
   *  gets drawn as the same mountain range as one that pinned the box all minute. The
   *  peak/low axis still reports the DATA's own range, so nothing is hidden by it. */
  scale?: { min: number; max: number };
}

const W = 100;
const H = 32;

function xAt(i: number, n: number): number {
  return n > 1 ? (i / (n - 1)) * W : W / 2;
}

/** Reserved inside the viewBox so a value AT either end of the scale still draws a whole
 *  stroke. Without it a series sitting at its floor — a token rate of exactly zero, the
 *  commonest case — lands on y = H, half the stroke falls outside the box and is clipped,
 *  and a real zero becomes indistinguishable from no data at all. That is the one thing
 *  these plots must never blur: a gap means "we were not told", a floor means "it was
 *  zero". A value at the top was clipped the same way. */
const EDGE_PAD = 1;

function yAt(v: number, min: number, max: number): number {
  const span = max - min || 1;
  return H - EDGE_PAD - ((v - min) / span) * (H - 2 * EDGE_PAD);
}

function linePath(values: (number | null)[], min: number, max: number): string {
  const n = values.length;
  let d = "";
  let penDown = false;
  values.forEach((v, i) => {
    if (v == null) {
      penDown = false;
      return;
    }
    d += `${penDown ? "L" : "M"}${xAt(i, n).toFixed(2)} ${yAt(v, min, max).toFixed(2)} `;
    penDown = true;
  });
  return d.trim();
}

/** The area between the line and the floor, as one path per unbroken run.
 *
 *  Per RUN, not one path for the whole series: closing across a gap would shade a
 *  stretch where there was no reading, turning "we weren't told" into "it was zero". */
function areaPath(values: (number | null)[], min: number, max: number): string {
  const n = values.length;
  let d = "";
  let run: [number, number][] = [];
  const close = (): void => {
    const first = run[0];
    const last = run[run.length - 1];
    if (first !== undefined && last !== undefined) {
      const points = run.map(([x, y]) => `${x.toFixed(2)} ${y.toFixed(2)}`).join(" L");
      const floor = (H - EDGE_PAD).toFixed(2);
      d += `M${first[0].toFixed(2)} ${floor} L${points} L${last[0].toFixed(2)} ${floor} Z `;
    }
    run = [];
  };
  values.forEach((v, i) => {
    if (v == null) {
      close();
      return;
    }
    run.push([xAt(i, n), yAt(v, min, max)]);
  });
  close();
  return d.trim();
}

function latestOf(values: (number | null)[]): number | null {
  for (let i = values.length - 1; i >= 0; i -= 1) {
    const v = values[i];
    if (v != null) return v;
  }
  return null;
}

function Sparkline({ label, lines, fmt, scale }: PlotSeries): ReactNode {
  // A shared Y-scale across every line AND its peak line in the panel, so two
  // series (down/up) are comparable and the peak line fits. Peak/low span the
  // whole panel — with a band present, "peak" is the true bucket-max, not the avg.
  const present = lines.flatMap((l) =>
    [...l.values, ...(l.band ?? [])].filter((v): v is number => v != null),
  );
  if (present.length === 0) return null;
  const low = Math.min(...present);
  const high = Math.max(...present);
  // The axis always reports the DATA's range; only the drawing is pinned when the
  // caller gives a bounded scale, so a flat 1% line still says what it was.
  const min = scale ? scale.min : low;
  const max = scale ? scale.max : high;
  const multi = lines.length > 1;
  return (
    <div className="plot">
      <div className="plot-head">
        <span className="plot-label">{label}</span>
        <span className="plot-now">
          {lines.map((l, i) => {
            const latest = latestOf(l.values);
            if (latest == null) return null;
            return (
              <span className="plot-now-item" key={l.label ?? i}>
                {multi && (
                  <span
                    className="plot-swatch"
                    style={{ background: l.color }}
                    aria-hidden="true"
                  />
                )}
                {multi && l.label && <span className="plot-sublabel">{l.label}</span>}
                {fmt(latest)}
              </span>
            );
          })}
        </span>
      </div>
      <svg
        className="plot-svg"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        {lines.map((l, i) =>
          l.fill === true ? (
            <path
              key={`area-${l.label ?? i}`}
              d={areaPath(l.values, min, max)}
              fill={l.color}
              fillOpacity={0.16}
              stroke="none"
            />
          ) : null,
        )}
        {lines.map((l, i) =>
          l.band?.some((v) => v != null) ? (
            <path
              key={`peak-${l.label ?? i}`}
              d={linePath(l.band, min, max)}
              fill="none"
              stroke={l.color}
              strokeWidth={1}
              strokeOpacity={0.5}
              vectorEffect="non-scaling-stroke"
            />
          ) : null,
        )}
        {lines.map((l, i) => (
          <path
            key={l.label ?? i}
            d={linePath(l.values, min, max)}
            fill="none"
            stroke={l.color}
            strokeWidth={1.5}
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>
      <div className="plot-axis">
        <span>{fmt(high)} peak</span>
        <span>{fmt(low)} low</span>
      </div>
    </div>
  );
}

/** A stack of labeled sparklines. Renders nothing when every series is empty. */
export function TimeSeriesPlot({ series }: { series: PlotSeries[] }): ReactNode {
  const drawn = series.filter((s) => s.lines.some((l) => l.values.some((v) => v != null)));
  if (drawn.length === 0) return null;
  return (
    <div className="plot-stack">
      {drawn.map((s) => (
        <Sparkline key={s.label} {...s} />
      ))}
    </div>
  );
}
