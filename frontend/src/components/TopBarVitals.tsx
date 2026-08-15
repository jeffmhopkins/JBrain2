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

import { useEffect, useRef, useState } from "react";

import { useTokenRate } from "../agent/tokenMeter";
import { useGpuBusy } from "../hostVitals";
import type { SyncStatus } from "../notes/useNotes";

/** Seconds of history on the axis. */
const SAMPLES = 12;
/** Column pitch and width in the SVG's own units. */
const PITCH = 4;
const BAR_W = 2.6;
/** Plot bounds: the baseline rule sits just under BOTTOM, which is the sync channel. */
const TOP = 1.4;
const BOTTOM = 16.6;
/** Full scale for the token trace. The two channels share a box but not a scale, so
 *  each is comparable against ITSELF over time and never against the other. */
const TPS_FULL_SCALE = 140;
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
}

export function TopBarVitals({ syncStatus }: VitalsProps) {
  const { percent: gpu, state: gauge } = useGpuBusy();
  const tps = useTokenRate();
  const [gpuHistory, tpsHistory] = useVitalsHistory(gpu, tps, syncStatus);

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

  return (
    // <output> carries an implicit `status` role, so the chart announces itself as a
    // live region without a redundant role — this is a readout, never a control.
    <output
      className="vitals"
      data-turn={live ? "live" : "idle"}
      data-sync={syncStatus}
      data-gpu={hasGpu ? "on" : "off"}
      aria-label={label}
      title={label}
    >
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
          <span className="vitals-value">{hasGpu ? Math.round(gpu) : ""}</span>
          <span className="vitals-unit">{hasGpu ? "%" : gauge === "absent" ? "no gpu" : ""}</span>
        </span>
      </div>
    </output>
  );
}

/** The token trace, with a break wherever the turn wasn't producing — a gap is the
 *  honest mark for "nothing was generated then", which a line through zero is not. */
function tracePath(history: (number | null)[]): string {
  let path = "";
  let open = false;
  history.forEach((value, i) => {
    if (value === null) {
      open = false;
      return;
    }
    const [x, y] = tracePoint(value, i);
    path += `${open ? "L" : "M"}${x} ${y} `;
    open = true;
  });
  return path.trim();
}

/** The dot on the newest token sample, so "now" is findable on a 48px-wide chart. */
function traceHead(history: (number | null)[]) {
  const latest = history[history.length - 1];
  if (latest === null || latest === undefined) return null;
  const [x, y] = tracePoint(latest, history.length - 1);
  return <circle className="vitals-trace-head" cx={x} cy={y} r="1.15" />;
}

function tracePoint(value: number, index: number): [string, string] {
  const clamped = Math.max(0, Math.min(value, TPS_FULL_SCALE));
  const y = BOTTOM - (clamped / TPS_FULL_SCALE) * (BOTTOM - TOP);
  return [(index * PITCH + 0.2 + BAR_W / 2).toFixed(2), y.toFixed(2)];
}

/** Both channels sampled onto ONE clock, so the chart has a single honest time axis.
 *  The GPU stream and the token meter each publish at 1 Hz but on their own timers;
 *  sampling whatever is current on a tick of our own keeps one column per second
 *  rather than letting arrival jitter decide the spacing.
 *
 *  Frozen while the server is unreachable: with nothing arriving, advancing the axis
 *  would draw a growing run of blanks that reads as "the box went idle" when it means
 *  "we stopped being told". The trace holds, and the CSS dims it as stale. */
function useVitalsHistory(
  gpu: number | null,
  tps: number | null,
  syncStatus: SyncStatus,
): [(number | null)[], (number | null)[]] {
  const [history, setHistory] = useState<[(number | null)[], (number | null)[]]>(() => [
    new Array<number | null>(SAMPLES).fill(null),
    new Array<number | null>(SAMPLES).fill(null),
  ]);
  // The tick reads the latest values without re-arming the interval on every sample.
  const latest = useRef({ gpu, tps, syncStatus });
  latest.current = { gpu, tps, syncStatus };

  useEffect(() => {
    const timer = setInterval(() => {
      const now = latest.current;
      if (now.syncStatus === "unreachable") return;
      setHistory(([gpuPast, tpsPast]) => [
        [...gpuPast.slice(1), now.gpu],
        [...tpsPast.slice(1), now.tps],
      ]);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  return history;
}
