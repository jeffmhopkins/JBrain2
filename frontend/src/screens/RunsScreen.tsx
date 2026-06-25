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

function isToday(iso: string): boolean {
  const d = new Date(iso);
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
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
  runs: RunSummary[];
  /** Jobs waiting in app.jobs (GET /api/runs/queue-depth); null until it loads. */
  queueDepth: number | null;
}

/** The status-tile grid: active now / failed today / queued / tokens today — all
 * derived honestly from the run log, plus the live job-queue depth. */
function StatusTiles({ runs, queueDepth }: TilesProps) {
  // 'running' only — a 'queued' run is waiting, not active (the queue tile counts it).
  const active = runs.filter((r) => r.status === "running").length;
  const failedToday = runs.filter((r) => r.status === "error" && isToday(r.started_at)).length;
  const tokensToday = runs
    .filter((r) => isToday(r.started_at))
    .reduce((sum, r) => sum + r.cost_tokens, 0);
  return (
    <div className="runs-tiles">
      <div className="runs-tile runs-tile-running">
        <span className="runs-tile-icon">
          <RefreshIcon size={14} />
        </span>
        <span className="runs-tile-num">{active}</span>
        <span className="runs-tile-label">runs active now</span>
      </div>
      <div className="runs-tile runs-tile-failed">
        <span className="runs-tile-icon">
          <AlertTriangleIcon size={14} />
        </span>
        <span className="runs-tile-num">{failedToday}</span>
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
        <span className="runs-tile-num">{fmtTokens(tokensToday)}</span>
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

interface RunsScreenProps {
  onClose: () => void;
}

export function RunsScreen({ onClose }: RunsScreenProps) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [queueDepth, setQueueDepth] = useState<number | null>(null);
  const [sweeps, setSweeps] = useState<SweepTrigger[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<RunSummary | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
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
    try {
      setRuns(await api.runs());
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Live updates: while any run is in flight (and the tab is foreground), re-pull
  // the list — and the open run's detail — every few seconds so status, duration,
  // and tokens tick up without a manual refresh. Stops the moment nothing is
  // running (a backgrounded app suspends the poll, like the LLM-settings drawer).
  const foreground = useForeground();
  // Poll while anything is in flight OR waiting — a queued run will flip to running
  // and on to done, and the queue tile drains, all without a manual refresh.
  const anyActive = (runs ?? []).some((r) => r.status === "running" || r.status === "queued");
  useEffect(() => {
    if (!foreground || !anyActive) return;
    const tick = () => {
      api
        .runs()
        .then((fresh) => {
          setRuns(fresh);
          // Keep the open run's header live by re-deriving it from the fresh list.
          setSelected((cur) => (cur ? (fresh.find((r) => r.id === cur.id) ?? cur) : cur));
        })
        .catch(() => {});
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
  }, [foreground, anyActive, selected]);

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

        {runs !== null && <StatusTiles runs={runs} queueDepth={queueDepth} />}

        <SweepRow sweeps={sweeps} onFire={(t) => void fireSweep(t)} />

        <h3 className="runs-sect">Recent runs</h3>
        {runs === null && error === null ? (
          <p className="muted">Loading runs…</p>
        ) : runs !== null && runs.length === 0 ? (
          <p className="muted runs-empty">No runs yet — they appear here as the engine works.</p>
        ) : (
          <div className="runs-card">
            {runs?.map((run) => (
              <RunRow key={run.id} run={run} onOpen={openRun} />
            ))}
          </div>
        )}
      </div>

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
