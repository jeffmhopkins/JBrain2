// A reusable, presentational time-series plot: a stack of labeled sparklines.
// Each series renders one mini line chart (current-value readout + peak/low axis)
// in a 100×32 viewBox stretched to the container; a null sample breaks the line
// (a gap, not a drop to zero), and an all-null series is omitted. Callers own the
// palette and formatters — the component never reads model data directly, so it's
// safe to drive from both the Ops screen and an agent tool view.

import type { ReactNode } from "react";

export interface PlotSeries {
  /** Row label, shown left of the current value. */
  label: string;
  /** Stroke color — a CSS token the caller owns (never model-authored). */
  color: string;
  /** One value per bucket; null breaks the line rather than dropping to zero. */
  values: (number | null)[];
  /** Formats the current / peak / low readouts. */
  fmt: (v: number) => string;
}

const W = 100;
const H = 32;

function linePath(values: (number | null)[], min: number, max: number): string {
  const span = max - min || 1;
  const n = values.length;
  let d = "";
  let penDown = false;
  values.forEach((v, i) => {
    if (v == null) {
      penDown = false;
      return;
    }
    const x = n > 1 ? (i / (n - 1)) * W : W / 2;
    const y = H - ((v - min) / span) * H;
    d += `${penDown ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)} `;
    penDown = true;
  });
  return d.trim();
}

function Sparkline({ label, color, values, fmt }: PlotSeries): ReactNode {
  const present = values.filter((v): v is number => v != null);
  if (present.length === 0) return null;
  const min = Math.min(...present);
  const max = Math.max(...present);
  // present is non-empty (guarded above); `?? max` only satisfies the indexer type.
  const latest = present[present.length - 1] ?? max;
  return (
    <div className="plot">
      <div className="plot-head">
        <span className="plot-label">{label}</span>
        <span className="plot-now">{fmt(latest)}</span>
      </div>
      <svg
        className="plot-svg"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <path
          d={linePath(values, min, max)}
          fill="none"
          stroke={color}
          strokeWidth={1.5}
          vectorEffect="non-scaling-stroke"
        />
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
  const drawn = series.filter((s) => s.values.some((v) => v != null));
  if (drawn.length === 0) return null;
  return (
    <div className="plot-stack">
      {drawn.map((s) => (
        <Sparkline key={s.label} {...s} />
      ))}
    </div>
  );
}
