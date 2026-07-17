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
  /** Optional per-bucket peak (bucket max), drawn as a faint filled envelope under
   * the line so a spike shorter than a bucket isn't averaged out of view. */
  band?: (number | null)[];
}

export interface PlotSeries {
  /** Row label, shown left of the current value(s). */
  label: string;
  /** One or more lines sharing this panel's Y-scale. */
  lines: PlotLine[];
  /** Formats the current / peak / low readouts. */
  fmt: (v: number) => string;
}

const W = 100;
const H = 32;

function xAt(i: number, n: number): number {
  return n > 1 ? (i / (n - 1)) * W : W / 2;
}

function yAt(v: number, min: number, max: number): number {
  return H - ((v - min) / (max - min || 1)) * H;
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

// A filled area from the baseline (chart floor) up to each band value — the peak
// envelope. Each run of non-null samples is its own closed sub-path, so a gap
// stays a gap rather than bridging the fill across missing buckets.
function bandPath(values: (number | null)[], min: number, max: number): string {
  const n = values.length;
  let d = "";
  let start = -1;
  const flush = (end: number) => {
    if (start < 0) return;
    let seg = "";
    for (let i = start; i <= end; i += 1) {
      const v = values[i];
      if (v == null) continue;
      seg += `${seg ? "L" : "M"}${xAt(i, n).toFixed(2)} ${yAt(v, min, max).toFixed(2)} `;
    }
    d += `${seg}L${xAt(end, n).toFixed(2)} ${H} L${xAt(start, n).toFixed(2)} ${H} Z `;
    start = -1;
  };
  values.forEach((v, i) => {
    if (v == null) {
      flush(i - 1);
    } else if (start < 0) {
      start = i;
    }
  });
  flush(n - 1);
  return d.trim();
}

function latestOf(values: (number | null)[]): number | null {
  for (let i = values.length - 1; i >= 0; i -= 1) {
    const v = values[i];
    if (v != null) return v;
  }
  return null;
}

function Sparkline({ label, lines, fmt }: PlotSeries): ReactNode {
  // A shared Y-scale across every line AND its peak band in the panel, so two
  // series (down/up) are comparable and the fill fits. Peak/low span the whole
  // panel — with a band present, "peak" is the true bucket-max, not the avg.
  const present = lines.flatMap((l) =>
    [...l.values, ...(l.band ?? [])].filter((v): v is number => v != null),
  );
  if (present.length === 0) return null;
  const min = Math.min(...present);
  const max = Math.max(...present);
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
          l.band?.some((v) => v != null) ? (
            <path
              key={`band-${l.label ?? i}`}
              d={bandPath(l.band, min, max)}
              fill={l.color}
              fillOpacity={0.16}
              stroke="none"
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
        <span>{fmt(max)} peak</span>
        <span>{fmt(min)} low</span>
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
