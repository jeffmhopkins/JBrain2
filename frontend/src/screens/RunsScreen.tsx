// The Ops "Runs" surface — Direction C (docs/mocks/runs-ops-c-dashboard-split.html,
// the binding spec). An ops dashboard: glanceable status tiles, a prominent
// sweep-control row for emergency triggers, then the compact run log. Tapping a
// run raises the shared bottom Sheet (the mock's "split panel" — half-height
// over the still-visible, dimmed list) showing its step tree with ok/error
// nodes and a failing step's error. Tokens only, honest live status: an in-flight
// pipeline whose steps have not started reads 'queued' (it waits behind the
// single-threaded worker), and the queue tile shows the live job backlog (GET
// /api/runs/queue-depth). Reachable from Ops.

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  type RunDetail,
  type RunListParams,
  type RunStats,
  type RunStatus,
  type RunSummary,
  type SweepTrigger,
  api,
} from "../api/client";
import { Sheet } from "../components/Sheet";
import {
  AlertTriangleIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CoinsIcon,
  FilterIcon,
  ListIcon,
  PlusIcon,
  RefreshIcon,
  XIcon,
} from "../components/icons";
import { useForeground } from "../visibility";
import { fmtTokens } from "./aiUsage";

/** 'error' is the stored failed state (migration 0016); the surface renders it
 * as the red "failed" tile/dot. This is the one place the mapping lives. */
function statusLabel(status: RunStatus): string {
  return status === "error" ? "failed" : status;
}

function fmtDuration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms}ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function fmtAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const s = Math.round(diff / 1000);
  if (s < 45) return "now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "Request failed. Is the server reachable?";
}

/** One captured log event as a compact line: HH:MM:SS, the event message, then its
 * remaining structured fields as k=v — the "full logs" review trace. */
function fmtLogEvent(ev: Record<string, unknown>): string {
  const { event, timestamp, level, ...rest } = ev;
  const time = typeof timestamp === "string" ? timestamp.slice(11, 19) : "";
  const fields = Object.entries(rest)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" ");
  return [time, String(event ?? ""), fields].filter(Boolean).join("  ");
}

interface TilesProps {
  /** Aggregates over the whole log (GET /api/runs/stats); null until it loads. */
  stats: RunStats | null;
  /** Jobs waiting in app.jobs (GET /api/runs/queue-depth); null until it loads. */
  queueDepth: number | null;
}

/** The status-tile grid: active now / failed today / queued / tokens today. These
 * come from the server-side stats aggregate (the whole log), so they stay honest
 * while the list below is filtered — and "tokens today" reflects the day, not just
 * the fetched page. */
function StatusTiles({ stats, queueDepth }: TilesProps) {
  const num = (n: number | undefined) => (n === undefined ? "—" : String(n));
  return (
    <div className="runs-tiles">
      <div className="runs-tile runs-tile-running">
        <span className="runs-tile-icon">
          <RefreshIcon size={14} />
        </span>
        <span className="runs-tile-num">{num(stats?.active)}</span>
        <span className="runs-tile-label">runs active now</span>
      </div>
      <div className="runs-tile runs-tile-failed">
        <span className="runs-tile-icon">
          <AlertTriangleIcon size={14} />
        </span>
        <span className="runs-tile-num">{num(stats?.failed_today)}</span>
        <span className="runs-tile-label">failed today</span>
      </div>
      <div className="runs-tile runs-tile-queue">
        <span className="runs-tile-icon">
          <ListIcon size={14} />
        </span>
        <span className="runs-tile-num">{queueDepth === null ? "—" : queueDepth}</span>
        <span className="runs-tile-label">jobs queued</span>
      </div>
      <div className="runs-tile runs-tile-cost">
        <span className="runs-tile-icon">
          <CoinsIcon size={14} />
        </span>
        <span className="runs-tile-num">
          {stats === null ? "—" : fmtTokens(stats.tokens_today)}
        </span>
        <span className="runs-tile-label">tokens today</span>
      </div>
    </div>
  );
}

interface SweepRowProps {
  sweeps: SweepTrigger[];
  onFire: (trigger: SweepTrigger) => void;
}

/** The prominent one-tap sweep-control row. Hidden entirely when no manual
 * triggers are exposed yet (sibling Track B's list endpoint). */
function SweepRow({ sweeps, onFire }: SweepRowProps) {
  if (sweeps.length === 0) return null;
  return (
    <>
      <h3 className="runs-sect">Run a sweep now</h3>
      <div className="runs-sweeprow">
        {sweeps.map((s) => (
          <button key={s.id} type="button" className="runs-sweepbtn" onClick={() => onFire(s)}>
            <RefreshIcon size={16} />
            {s.label ?? s.pipeline}
          </button>
        ))}
      </div>
    </>
  );
}

const KIND_CHIPS = new Set(["agent", "integration", "pipeline"]);
function kindClass(kind: string): string {
  return KIND_CHIPS.has(kind) ? kind : "pipeline";
}

// ===== Filtering (docs/reference/DESIGN.md "Runs — filtering"; mock B) =====
// Filtering is server-side (GET /api/runs): the filter state maps to query params so
// picking a kind fetches that kind from the FULL history, not just whatever survived
// the reconcile noise in the recent 50. The status tiles + chip counts come from a
// separate stats aggregate (over the whole log), so they stay honest while the list
// is filtered. Default is everything shown — the surface behaves as before until a
// control is touched.

type ChipKey = "agent" | "integration" | "pipeline";
/** Each chip expands to the run kinds it fetches: a subagent run is an agent turn's
 * child, so it rides the Agent chip. */
const CHIP_DEFS: { key: ChipKey; label: string; kinds: string[] }[] = [
  { key: "agent", label: "Agent", kinds: ["agent", "subagent"] },
  { key: "integration", label: "Integration", kinds: ["integration"] },
  { key: "pipeline", label: "Pipeline", kinds: ["pipeline"] },
];

const DAY_MS = 86_400_000;
// "All" limit fetches up to the server's MAX_LIMIT (the API clamps there anyway).
const ALL_LIMIT = 200;

const RANGES: { label: string; days: number }[] = [
  { label: "Today", days: 1 },
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "All", days: Number.POSITIVE_INFINITY },
];
const LIMITS: { label: string; n: number }[] = [
  { label: "25", n: 25 },
  { label: "50", n: 50 },
  { label: "All", n: Number.POSITIVE_INFINITY },
];

interface RunFilter {
  show: Record<ChipKey, boolean>;
  rangeDays: number;
  limit: number;
  hideSweeps: boolean;
}
const DEFAULT_FILTER: RunFilter = {
  show: { agent: true, integration: true, pipeline: true },
  rangeDays: Number.POSITIVE_INFINITY,
  limit: 50,
  hideSweeps: false,
};

/** How many *sheet* filters are non-default — drives the filter button's badge.
 * The kind chips are their own visible control and don't count here. */
function sheetFilterCount(f: RunFilter): number {
  return (
    (Number.isFinite(f.rangeDays) ? 1 : 0) +
    (f.limit !== DEFAULT_FILTER.limit ? 1 : 0) +
    (f.hideSweeps ? 1 : 0)
  );
}
function anyKindHidden(f: RunFilter): boolean {
  return CHIP_DEFS.some((c) => !f.show[c.key]);
}

/** The enabled chips, in display order (also the ones whose counts sum to the total). */
function enabledChips(f: RunFilter) {
  return CHIP_DEFS.filter((c) => f.show[c.key]);
}

/** The date-range floor as an ISO string, or undefined for "all time". */
function sinceIso(f: RunFilter): string | undefined {
  return Number.isFinite(f.rangeDays)
    ? new Date(Date.now() - f.rangeDays * DAY_MS).toISOString()
    : undefined;
}

/** Map the filter state to the list endpoint's query params. `kinds` is omitted when
 * every chip is on (fetch all, including any unknown kind); a fully-off selection is
 * handled by the caller (it fetches nothing rather than sending an empty kinds list,
 * which the API would read as "no filter"). */
function listParams(f: RunFilter): RunListParams {
  const enabled = enabledChips(f);
  const p: RunListParams = { limit: Number.isFinite(f.limit) ? f.limit : ALL_LIMIT };
  if (enabled.length !== CHIP_DEFS.length) p.kinds = enabled.flatMap((c) => c.kinds);
  if (f.hideSweeps) p.excludeSweeps = true;
  const since = sinceIso(f);
  if (since) p.since = since;
  return p;
}

/** The stats query params — the list's date-range + hide-sweeps (kinds don't scope
 * the per-kind counts, so they're never sent here). */
function statsParams(f: RunFilter): { excludeSweeps?: boolean; since?: string } {
  const p: { excludeSweeps?: boolean; since?: string } = {};
  if (f.hideSweeps) p.excludeSweeps = true;
  const since = sinceIso(f);
  if (since) p.since = since;
  return p;
}

interface RunRowProps {
  run: RunSummary;
  onOpen: (run: RunSummary) => void;
}

function RunRow({ run, onOpen }: RunRowProps) {
  return (
    <button type="button" className="runs-row" onClick={() => onOpen(run)}>
      <span className={`runs-dot runs-dot-${run.status}`} aria-hidden="true" />
      <span className="runs-row-main">
        <span className="runs-row-name">
          <span className={`runs-kind runs-kind-${kindClass(run.kind)}`}>{run.kind}</span>
          {run.name}
        </span>
        <span className="runs-row-sub">
          {run.status === "queued" ? (
            // Nothing has run yet — there is no duration/token summary to show.
            <>waiting to start · {run.step_count} steps</>
          ) : run.status === "running" && run.progress_note ? (
            // While in flight, the live "processed X of Y" line is the useful thing to
            // show; the duration/tokens summary lands when the run closes.
            run.progress_note
          ) : (
            <>
              {fmtDuration(run.duration_ms)} · {run.step_count} steps · {fmtTokens(run.cost_tokens)}{" "}
              tok
              {run.last_error ? ` · ${run.last_error}` : ""}
            </>
          )}
        </span>
      </span>
      <span className="runs-row-right">
        <span>{fmtAgo(run.started_at)}</span>
        <span className={`runs-status runs-status-${run.status}`}>{statusLabel(run.status)}</span>
      </span>
      <ChevronRightIcon size={15} />
    </button>
  );
}

function stepNode(ok: boolean) {
  return ok ? (
    <span className="runs-snode runs-snode-ok">
      <CheckIcon size={11} />
    </span>
  ) : (
    <span className="runs-snode runs-snode-err">
      <XIcon size={11} />
    </span>
  );
}

interface DetailProps {
  run: RunSummary;
  detail: RunDetail | null;
  error: string | null;
  onClose: () => void;
  onRerun: (run: RunSummary) => void;
}

/** The split panel: the run header (name + status badge + meta), the step tree,
 * and the View-log / Re-run footer, hosted by the shared bottom Sheet (the
 * mock's half-height sheet over the dimmed list). */
function RunDetailSheet({ run, detail, error, onClose, onRerun }: DetailProps) {
  return (
    <Sheet title={run.name} onClose={onClose}>
      <div className="runs-sp-head">
        <span className={`runs-badge runs-badge-${run.status}`}>{statusLabel(run.status)}</span>
        <span className="runs-sp-meta">
          {run.kind} · {fmtAgo(run.started_at)} · {fmtDuration(run.duration_ms)} ·{" "}
          {fmtTokens(run.cost_tokens)} tok
        </span>
        {run.status === "running" && run.progress_note && (
          <span className="runs-sp-progress">{run.progress_note}</span>
        )}
      </div>
      <div className="runs-sp-body">
        {error !== null ? (
          <p className="error" role="alert">
            {error}
          </p>
        ) : detail === null ? (
          <p className="muted">Loading steps…</p>
        ) : detail.steps.length === 0 ? (
          <p className="muted runs-empty">No steps recorded for this run.</p>
        ) : (
          detail.steps.map((step) => (
            <div key={step.idx} className="runs-strow">
              {stepNode(step.ok)}
              <div className="runs-smain">
                <div className="runs-sline">
                  <span className={`runs-skind runs-skind-${kindClass(step.kind)}`}>
                    {step.kind}
                  </span>
                  <span className="runs-sname">{step.name}</span>
                  <span className="runs-scost">{fmtTokens(step.cost_tokens)}</span>
                </div>
                {step.error !== null && <div className="runs-serr">{step.error}</div>}
                {step.detail && step.detail.length > 0 && (
                  <details className="runs-slog">
                    <summary>
                      {step.detail.length} log {step.detail.length === 1 ? "event" : "events"}
                    </summary>
                    <pre className="runs-slog-body">
                      {step.detail.map((ev) => fmtLogEvent(ev)).join("\n")}
                    </pre>
                  </details>
                )}
              </div>
            </div>
          ))
        )}
      </div>
      <div className="runs-sp-foot">
        <button type="button" className="runs-rerun" onClick={() => onRerun(run)}>
          <RefreshIcon size={15} />
          Re-run
        </button>
      </div>
    </Sheet>
  );
}

interface FilterBarProps {
  filter: RunFilter;
  /** Per-kind counts within the active range (after any hide-sweeps), for the chip pills. */
  counts: Record<ChipKey, number>;
  onToggleKind: (key: ChipKey) => void;
  onOpenSheet: () => void;
}

/** The multi-select show/hide chip row + the filter-sheet button (mock B). */
function FilterBar({ filter, counts, onToggleKind, onOpenSheet }: FilterBarProps) {
  const badge = sheetFilterCount(filter);
  return (
    <div className="runs-filterbar">
      <div className="runs-chips">
        {CHIP_DEFS.map(({ key, label }) => (
          <button
            key={key}
            type="button"
            className={`runs-kindchip ${key}`}
            aria-pressed={filter.show[key]}
            onClick={() => onToggleKind(key)}
          >
            <span className="runs-kindchip-dot" aria-hidden="true" />
            {label}
            <span className="runs-kindchip-n">{counts[key]}</span>
          </button>
        ))}
      </div>
      <button
        type="button"
        className={`runs-filtbtn${badge > 0 ? " runs-filtbtn-on" : ""}`}
        onClick={onOpenSheet}
        aria-label="Filter runs"
      >
        <FilterIcon size={18} />
        {badge > 0 && <span className="runs-filt-badge">{badge}</span>}
      </button>
    </div>
  );
}

interface FilterSheetProps {
  filter: RunFilter;
  onChange: (next: RunFilter) => void;
  onClose: () => void;
}

/** Date range, result limit, and the hide-reconcile-sweeps convenience — the
 * lower-frequency controls tucked behind the filter button (mock B). Changes
 * apply live; the sheet composes the shared Sheet + settled .seg-row control. */
function FilterSheet({ filter, onChange, onClose }: FilterSheetProps) {
  return (
    <Sheet title="Filter runs" onClose={onClose}>
      <div className="runs-fil">
        <div className="runs-fil-group">
          <div className="runs-fil-label">Date range</div>
          <div className="seg-row">
            {RANGES.map((r) => (
              <button
                key={r.label}
                type="button"
                className={`seg${filter.rangeDays === r.days ? " seg-on" : ""}`}
                aria-pressed={filter.rangeDays === r.days}
                onClick={() => onChange({ ...filter, rangeDays: r.days })}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
        <div className="runs-fil-group">
          <div className="runs-fil-label">Show at most</div>
          <div className="seg-row">
            {LIMITS.map((l) => (
              <button
                key={l.label}
                type="button"
                className={`seg${filter.limit === l.n ? " seg-on" : ""}`}
                aria-pressed={filter.limit === l.n}
                onClick={() => onChange({ ...filter, limit: l.n })}
              >
                {l.label}
              </button>
            ))}
          </div>
        </div>
        <div className="runs-fil-group">
          <button
            type="button"
            className="runs-fil-noise"
            aria-pressed={filter.hideSweeps}
            onClick={() => onChange({ ...filter, hideSweeps: !filter.hideSweeps })}
          >
            <span className="runs-fil-noise-txt">
              <span className="runs-fil-noise-t">Hide reconcile sweeps</span>
              <span className="runs-fil-noise-d">
                the ~0-token reconcile_* / geofence housekeeping runs
              </span>
            </span>
            <span className="auto-sw" aria-hidden="true" aria-checked={filter.hideSweeps}>
              <span className="auto-knob" />
            </span>
          </button>
        </div>
        <button type="button" className="runs-fil-done" onClick={onClose}>
          Done
        </button>
      </div>
    </Sheet>
  );
}

interface RunsScreenProps {
  onClose: () => void;
}

export function RunsScreen({ onClose }: RunsScreenProps) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [stats, setStats] = useState<RunStats | null>(null);
  const [queueDepth, setQueueDepth] = useState<number | null>(null);
  const [sweeps, setSweeps] = useState<SweepTrigger[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<RunSummary | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [filter, setFilter] = useState<RunFilter>(DEFAULT_FILTER);
  const [filterOpen, setFilterOpen] = useState(false);

  // The filtered list + the stats aggregate, both driven by the current filter. The
  // list reaches past the recency window server-side; the stats keep the tiles +
  // chip counts honest over the whole log. A fully-off chip selection fetches no
  // list (an empty kinds set would read as "no filter") but still pulls stats.
  const loadFiltered = useCallback(async (f: RunFilter) => {
    setError(null);
    api
      .runsStats(statsParams(f))
      .then(setStats)
      .catch(() => {});
    if (enabledChips(f).length === 0) {
      setRuns([]);
      return;
    }
    try {
      setRuns(await api.runs(listParams(f)));
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  // Filter-independent chrome: the sweep controls and the job-queue depth tile.
  const loadMeta = useCallback(() => {
    // The sweep list is sibling Track B's; treat its absence as "no sweeps".
    api
      .sweepTriggers()
      .then(setSweeps)
      .catch(() => setSweeps([]));
    // Best-effort: a missing/erroring queue-depth source leaves the tile at "—".
    api
      .queueDepth()
      .then(setQueueDepth)
      .catch(() => setQueueDepth(null));
  }, []);

  const refresh = useCallback(() => {
    loadMeta();
    void loadFiltered(filter);
  }, [loadMeta, loadFiltered, filter]);

  useEffect(() => {
    loadMeta();
  }, [loadMeta]);

  // Re-fetch the list + stats whenever the filter changes (and on first mount).
  useEffect(() => {
    void loadFiltered(filter);
  }, [filter, loadFiltered]);

  // Live updates: while any run is in flight (and the tab is foreground), re-pull
  // the list, stats, and the open run's detail every few seconds so status,
  // duration, and tokens tick up without a manual refresh. Stops the moment nothing
  // is running (a backgrounded app suspends the poll, like the LLM-settings drawer).
  const foreground = useForeground();
  // Poll while anything is in flight OR waiting — a queued job or running run will
  // flip to done, and the queue tile drains, all without a manual refresh. Read from
  // stats/queueDepth (not the filtered list), since the active run may be filtered out.
  const anyActive = (stats?.active ?? 0) > 0 || (queueDepth ?? 0) > 0;
  useEffect(() => {
    if (!foreground || !anyActive) return;
    const tick = () => {
      void loadFiltered(filter);
      api
        .queueDepth()
        .then(setQueueDepth)
        .catch(() => {});
      if (selected)
        api
          .run(selected.id)
          .then(setDetail)
          .catch(() => {});
    };
    const id = setInterval(tick, 3000);
    return () => clearInterval(id);
  }, [foreground, anyActive, selected, filter, loadFiltered]);

  const openRun = useCallback((run: RunSummary) => {
    setSelected(run);
    setDetail(null);
    setDetailError(null);
    api
      .run(run.id)
      .then(setDetail)
      .catch((err) => setDetailError(errorMessage(err)));
  }, []);

  function closeRun() {
    setSelected(null);
    setDetail(null);
    setDetailError(null);
  }

  async function fireSweep(trigger: SweepTrigger) {
    try {
      await api.runTrigger(trigger.id);
      setToast(`Fired "${trigger.label ?? trigger.pipeline}" — run queued.`);
      void refresh();
    } catch (err) {
      setToast(errorMessage(err));
    }
  }

  function rerun(_run: RunSummary) {
    closeRun();
    // A run is re-fired through the trigger that drove it; that mutation is the
    // sweep row (sibling Track B). We surface that honestly rather than no-op.
    setToast("To re-run, fire its pipeline from the sweep row above.");
  }

  function toggleKind(key: ChipKey) {
    setFilter((f) => ({ ...f, show: { ...f.show, [key]: !f.show[key] } }));
  }

  // The server did the filtering; the chip pills + count line read off the stats
  // aggregate (per-kind counts over the whole log, scoped to the active range +
  // hide-sweeps), so they agree with the filtered list without re-deriving it.
  const shown = runs ?? [];
  const counts: Record<ChipKey, number> = {
    agent: stats?.by_kind.agent ?? 0,
    integration: stats?.by_kind.integration ?? 0,
    pipeline: stats?.by_kind.pipeline ?? 0,
  };
  // Total matched = the enabled chips' counts; the list caps at the limit, so the
  // line reads "N of M" when the page is capped. Guard against a briefly-stale stat.
  const total = Math.max(
    enabledChips(filter).reduce((n, c) => n + counts[c.key], 0),
    shown.length,
  );
  const filtersActive = sheetFilterCount(filter) > 0 || anyKindHidden(filter);

  // The count line's descriptor tail (range · hidden kinds · sweeps).
  const parts: string[] = [];
  if (Number.isFinite(filter.rangeDays)) {
    parts.push(RANGES.find((r) => r.days === filter.rangeDays)?.label.toLowerCase() ?? "today");
  }
  const hidden = CHIP_DEFS.filter((c) => !filter.show[c.key]).map((c) => c.label.toLowerCase());
  if (hidden.length) parts.push(`${hidden.join(" + ")} hidden`);
  if (filter.hideSweeps) parts.push("sweeps hidden");
  const countN = shown.length === total ? `${total}` : `${shown.length} of ${total}`;
  const countText = `${countN} run${total === 1 ? "" : "s"}${parts.length ? ` · ${parts.join(" · ")}` : ""}`;

  return (
    // Runs can mount inside Ops's `.subscreen`, whose down-swipe dismiss
    // (App.tsx) would otherwise bubble through and climb out from under this
    // overlay. Swallow touch events here so that gesture never arms over Runs.
    <section
      className="runs-screen"
      onTouchStart={(e) => e.stopPropagation()}
      onTouchMove={(e) => e.stopPropagation()}
    >
      <header className="runs-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back to Ops">
          <ChevronLeftIcon size={22} />
        </button>
        <h2 className="runs-bar-title">Runs</h2>
        <button
          type="button"
          className="icon-btn runs-refresh"
          onClick={refresh}
          aria-label="Refresh"
        >
          <RefreshIcon size={20} />
        </button>
      </header>

      <div className="runs-body">
        {error !== null && (
          <p className="error" role="alert">
            {error}
          </p>
        )}

        <StatusTiles stats={stats} queueDepth={queueDepth} />

        <SweepRow sweeps={sweeps} onFire={(t) => void fireSweep(t)} />

        <h3 className="runs-sect">Recent runs</h3>
        {runs === null && error === null ? (
          <p className="muted">Loading runs…</p>
        ) : runs !== null && runs.length === 0 && !filtersActive ? (
          // An empty log (no filters applied) — nothing to filter, so no bar.
          <p className="muted runs-empty">No runs yet — they appear here as the engine works.</p>
        ) : (
          <>
            <FilterBar
              filter={filter}
              counts={counts}
              onToggleKind={toggleKind}
              onOpenSheet={() => setFilterOpen(true)}
            />
            <p className="runs-countline">
              {countText}
              {filtersActive && (
                <button
                  type="button"
                  className="runs-countline-reset"
                  onClick={() => setFilter(DEFAULT_FILTER)}
                >
                  reset
                </button>
              )}
            </p>
            {shown.length === 0 ? (
              <p className="muted runs-empty">
                No runs match — turn a chip back on, clear a filter, or widen the range.
              </p>
            ) : (
              <div className="runs-card">
                {shown.map((run) => (
                  <RunRow key={run.id} run={run} onOpen={openRun} />
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {filterOpen && (
        <FilterSheet filter={filter} onChange={setFilter} onClose={() => setFilterOpen(false)} />
      )}

      {selected !== null && (
        <RunDetailSheet
          run={selected}
          detail={detail}
          error={detailError}
          onClose={closeRun}
          onRerun={rerun}
        />
      )}

      {toast !== null && (
        <output className="runs-toast">
          <PlusIcon size={16} />
          {toast}
          <button
            type="button"
            className="runs-toast-x"
            onClick={() => setToast(null)}
            aria-label="Dismiss"
          >
            <XIcon size={14} />
          </button>
        </output>
      )}
    </section>
  );
}
