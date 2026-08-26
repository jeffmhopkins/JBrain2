// The top bar's right cluster: a 12-second strip chart of the box's vitals.
//
// Binding mock: docs/mocks/topbar-vitals/e-instrument.html (option E, chosen at the
// GUI gate over D "typographic" and F "stateful block"). GPU busy is a row of
// hairline columns that ticks left once a second; while an agent turn streams, a
// steel trace of tokens/sec is drawn over the same time axis. The shape carries
// "the box woke up" or "the stream stalled" a beat before the digits do.
//
// Sync survives the deleted 8px dot as the chart's own BASELINE RULE — but only
// when there is something to say. A healthy connection is silent: neutral rail, no
// word. Degraded states colour the rule AND show the word beneath it, so colour is
// never the only encoding (DESIGN.md), and the loud case still raises the status
// banner. The full state is always in the aria-label, healthy included.

import { useEffect, useReducer } from "react";

import { useTokenRate } from "../agent/tokenMeter";
import { latestVitals, perSecond, useGpuBusy } from "../hostVitals";
import type { SyncStatus } from "../notes/useNotes";

/** Seconds of history on the axis. */
const SAMPLES = 12;
/** Column pitch and width in the SVG's own units. */
const PITCH = 4;
const BAR_W = 2.6;
/** Plot bounds: the baseline rule sits just under BOTTOM, which is the sync channel. */
const TOP = 1.4;
const BOTTOM = 16.6;
/** Floor under the token trace's auto-scale, so a lone slow sample doesn't fill the box.
 *
 *  The trace scales to the WINDOW's own peak, not a fixed ceiling. It used to be pinned at
 *  140 tok/s, which meant a turn cruising at 20 — a perfectly normal rate for a big local
 *  model — drew a 14%-high squiggle flat against the baseline, indistinguishable from
 *  nothing happening. The two channels still share a box but not a scale, so each is read
 *  against ITSELF over time and never against the other; the number beside the trace is
 *  what conveys magnitude, and the trace conveys SHAPE. Same reasoning as the auto-fitted
 *  Ops sparklines (TimeSeriesPlot). */
const TPS_MIN_SCALE = 10;
/** GPU load that reads as "pinned" and turns the column amber. */
const HOT_PERCENT = 85;

const SYNC_WORD: Record<SyncStatus, string> = {
  synced: "synced",
  pending: "pending",
  unreachable: "offline",
};

const SYNC_FULL: Record<SyncStatus, string> = {
  synced: "synced",
  pending: "sync pending",
  unreachable: "server unreachable",
};

interface VitalsProps {
  syncStatus: SyncStatus;
  /** Opens the vitals detail surface. When given, the chart becomes a control: the
   *  drawing is untouched, but a 44px hit area wraps it (the chart itself is 48×20,
   *  under the minimum) and the live region gives way to a button — a control that
   *  also announces itself on every tick would be unusable with a screen reader. */
  onOpen?: (() => void) | undefined;
}

export function TopBarVitals({ syncStatus, onOpen }: VitalsProps) {
  const { percent: gpu, state: gauge } = useGpuBusy();
  const tps = useTokenRate();
  const [gpuHistory, tpsHistory] = useVitalsHistory();

  const hasGpu = gpu !== null;
  const live = tps !== null;
  // Three states, not two. "no gpu" is a claim about the BOX, so it is reserved for
  // the box actually saying so; a stream that is merely down says nothing at all
  // rather than accusing the hardware of being absent.
  const gpuLabel = hasGpu
    ? `GPU ${Math.round(gpu)}% busy`
    : gauge === "absent"
      ? "GPU gauge unavailable"
      : "GPU reading unavailable";
  const rateLabel = live ? ` · ${tps} tokens/sec` : "";
  const label = `${gpuLabel}${rateLabel} · ${SYNC_FULL[syncStatus]}`;

  // Two elements for one drawing. As a readout it is an <output> (implicit `status`
  // role, a live region). As a control it is a <button>: a live region that is also a
  // control announces the whole reading on every 1 Hz tick, which is unusable, so the
  // button carries a stable name and the drawing inside is hidden from the tree.
  const chart = (
    <>
      <div className="vitals-scope">
        <svg className="vitals-chart" viewBox="0 0 48 20" aria-hidden="true">
          <title>Box vitals, last 12 seconds</title>
          {gpuHistory.map((value, i) => {
            if (value === null) return null;
            const height = Math.max(0.9, (Math.min(value, 100) / 100) * (BOTTOM - TOP));
            return (
              <rect
                // biome-ignore lint/suspicious/noArrayIndexKey: position IS identity — each slot is a fixed second on the axis that samples shift through.
                key={`gpu-${i}`}
                className={`vitals-bar${value >= HOT_PERCENT ? " hot" : ""}`}
                x={(i * PITCH + 0.2).toFixed(2)}
                y={(BOTTOM - height).toFixed(2)}
                width={BAR_W}
                height={height.toFixed(2)}
                rx="0.4"
                // Older columns fade, so the eye reads the direction of travel.
                opacity={(0.3 + 0.7 * (i / (SAMPLES - 1))).toFixed(2)}
              />
            );
          })}
          {gauge === "absent" && (
            <line className="vitals-nogauge" x1="0.5" y1="9" x2="47.5" y2="9" />
          )}
          <path className="vitals-trace" d={tracePath(tpsHistory)} />
          {traceHead(tpsHistory)}
          <line className="vitals-rail" x1="0" y1="17.9" x2="48" y2="17.9" />
        </svg>
        <span className="vitals-sync">{SYNC_WORD[syncStatus]}</span>
      </div>
      <div className="vitals-reads">
        <span className="vitals-read vitals-tps">
          <span className="vitals-value">{live ? tps : ""}</span>
          <span className="vitals-unit">t/s</span>
        </span>
        <span className="vitals-read vitals-gpu">
          {/* An em dash for `unknown`, not a blank. A blank is indistinguishable from an
              idle box, which is how a stream that had died fatally went unnoticed: the
              chart drew no columns and the readout showed nothing, so a dead meter and a
              quiet one looked identical. DESIGN.md: honest status, always visible. */}
          <span className="vitals-value">
            {hasGpu ? Math.round(gpu) : gauge === "unknown" ? "—" : ""}
          </span>
          <span className="vitals-unit">{hasGpu ? "%" : gauge === "absent" ? "no gpu" : ""}</span>
        </span>
      </div>
    </>
  );

  const shared = {
    className: "vitals",
    "data-turn": live ? "live" : "idle",
    "data-sync": syncStatus,
    "data-gpu": hasGpu ? "on" : "off",
    title: label,
  } as const;

  if (onOpen) {
    return (
      <button type="button" {...shared} onClick={onOpen} aria-label={`Box vitals. ${label}`}>
        {chart}
      </button>
    );
  }
  return (
    <output {...shared} aria-label={label}>
      {chart}
    </output>
  );
}

/** The token trace, with a break wherever the turn wasn't producing — a gap is the
 *  honest mark for "nothing was generated then", which a line through zero is not. */
function tracePath(history: (number | null)[]): string {
  const scale = tpsScale(history);
  let path = "";
  let open = false;
  history.forEach((value, i) => {
    if (value === null) {
      open = false;
      return;
    }
    const [x, y] = tracePoint(value, i, scale);
    path += `${open ? "L" : "M"}${x} ${y} `;
    open = true;
  });
  return path.trim();
}

/** The dot on the newest token sample, so "now" is findable on a 48px-wide chart. */
function traceHead(history: (number | null)[]) {
  const latest = history[history.length - 1];
  if (latest === null || latest === undefined) return null;
  const [x, y] = tracePoint(latest, history.length - 1, tpsScale(history));
  return <circle className="vitals-trace-head" cx={x} cy={y} r="1.15" />;
}

/** The trace's ceiling: the tallest sample in the window, floored at TPS_MIN_SCALE. */
function tpsScale(history: (number | null)[]): number {
  let peak = TPS_MIN_SCALE;
  for (const value of history) {
    if (value !== null && value > peak) peak = value;
  }
  return peak;
}

function tracePoint(value: number, index: number, scale: number): [string, string] {
  const clamped = Math.max(0, Math.min(value, scale));
  const y = BOTTOM - (clamped / scale) * (BOTTOM - TOP);
  return [(index * PITCH + 0.2 + BAR_W / 2).toFixed(2), y.toFixed(2)];
}

const EMPTY = new Array<number | null>(SAMPLES).fill(null);

/** Both channels of the strip, read off the shared ring (hostVitals) at 1 Hz.
 *
 *  The strip used to keep a private 12-slot array, resampling whatever the stream had
 *  last published on a timer of its own — a second clock that could only disagree with
 *  the ring the vitals screen plots. Coming back from the background made the
 *  disagreement visible: the ring is backfilled from the box's record on reconnect, but
 *  a private array cannot be, so the reconnect seconds stayed holes in the strip while
 *  the detail graph filled in. Read off the ring, the strip gets the same repair — and
 *  the two surfaces can no longer tell two stories about the same twelve seconds.
 *
 *  The window is anchored at the newest sample, not the wall clock (`latestVitals`), so
 *  the strip FREEZES whenever frames stop arriving — an unreachable server, a suspended
 *  app — instead of draining: a growing run of blanks reads as "the box went idle" when
 *  it means "we stopped being told". The readout ("—") and the sync word carry the
 *  staleness, and the CSS dims the drawing off those states. */
function useVitalsHistory(): [(number | null)[], (number | null)[]] {
  // The ring is module state React cannot observe, so a 1 Hz tick forces the re-read
  // that advances the axis; renders from the live hooks land new frames sooner.
  const [, tick] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, []);

  const samples = latestVitals(SAMPLES);
  const newest = samples[samples.length - 1];
  if (newest === undefined) return [EMPTY, EMPTY];
  return [
    perSecond(samples, SAMPLES, (s) => s.gpu, newest.at),
    perSecond(samples, SAMPLES, (s) => s.tps, newest.at),
  ];
}
