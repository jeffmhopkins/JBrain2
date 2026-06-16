// The Ops "Workflow" surface — automation-centric (when -> do), binding mock
// docs/mocks/workflow-ops-a-automations-list.html. Every trigger reads as
// "when X -> run Y" with an enable toggle, status dot, next/last-run meta, and a
// Run-now (manual triggers only; event triggers are auto). Tapping a card expands
// to the pipeline's ordered steps (cost-class chip + description) + recent runs
// (a failed run shows its error). A secondary Catalog tab lists the actions.
// Reflects live engine state — the cards come straight from /api/ops/automations.

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  type Automation,
  type AutomationRun,
  type AutomationStep,
  type CatalogAction,
  type RunStatus,
  api,
} from "../api/client";
import {
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ListIcon,
  PlayIcon,
  RefreshIcon,
  XIcon,
  ZapIcon,
} from "../components/icons";

/** The mock's three sections, in order; an automation's `group` buckets it. */
const GROUPS: { key: Automation["group"]; label: string }[] = [
  { key: "event", label: "On a note event" },
  { key: "reconcile", label: "Reconcilers · every few minutes" },
  { key: "nightly", label: "Nightly sweeps" },
];

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "Request failed. Is the server reachable?";
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

function fmtIn(iso: string): string {
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return "due now";
  const s = Math.round(diff / 1000);
  const m = Math.round(s / 60);
  if (m < 60) return `in ${Math.max(1, m)}m`;
  const h = Math.round(m / 60);
  if (h < 48) return `in ${h}h`;
  return `in ${Math.round(h / 24)}d`;
}

function fmtInterval(seconds: number): string {
  if (seconds % 86400 === 0) {
    const d = seconds / 86400;
    return d === 1 ? "nightly" : `every ${d}d`;
  }
  if (seconds % 3600 === 0) return `every ${seconds / 3600}h`;
  if (seconds % 60 === 0) return `every ${seconds / 60}m`;
  return `every ${seconds}s`;
}

/** 'error' is the stored failed state; the surface renders it as "failed". */
function statusDotClass(status: RunStatus): string {
  return status === "error" ? "failed" : status === "running" ? "running" : "ok";
}

/** The when -> do headline. Event: "When <ev> → run <pipeline>"; schedule:
 * "<Interval> → run <pipeline>". */
function whenLine(a: Automation) {
  if (a.kind === "on_event") {
    return (
      <>
        When <span className="auto-ev">{a.on_event}</span> → run{" "}
        <span className="auto-pl">{a.pipeline}</span>
      </>
    );
  }
  const interval = a.interval_seconds !== null ? fmtInterval(a.interval_seconds) : "scheduled";
  const cap = interval.charAt(0).toUpperCase() + interval.slice(1);
  return (
    <>
      {cap} → run <span className="auto-pl">{a.pipeline}</span>
    </>
  );
}

/** The dim status + next/last meta line under the headline. */
function metaLine(a: Automation): string {
  const last = a.recent_runs[0];
  let status: string;
  if (!a.enabled) status = "idle · disabled";
  else if (last?.status === "running") status = "running now";
  else if (last?.status === "error") status = "last run failed";
  else if (last?.status === "done") status = "last run ok";
  else status = "idle";
  const parts = [status];
  if (a.kind === "schedule" && a.enabled && a.next_run_at !== null) {
    parts.push(`next ${fmtIn(a.next_run_at)}`);
  }
  if (a.manual) parts.push("manual");
  return parts.join(" · ");
}

function StepRow({ step, last }: { step: AutomationStep; last: boolean }) {
  return (
    <div className="auto-strow">
      <div className="auto-srail">
        <span className={`auto-snode${step.known ? " ok" : ""}`}>
          <CheckIcon size={11} />
        </span>
        {!last && <span className="auto-sconn" />}
      </div>
      <div className="auto-scontent">
        <span className="auto-saction">{step.action}</span>
        <span className={`auto-scost auto-scost-${step.cost_class}`}>{step.cost_class}</span>
        {step.description && <div className="auto-sdesc">{step.description}</div>}
        {!step.known && <div className="auto-sdesc auto-sdrift">not in the action registry</div>}
      </div>
    </div>
  );
}

function RunRow({ run }: { run: AutomationRun }) {
  return (
    <>
      <div className="auto-runrow">
        <span className={`auto-rd ${statusDotClass(run.status)}`} aria-hidden="true" />
        <span className="auto-rt">{fmtAgo(run.started_at)}</span>
        <span className="auto-rm">{fmtDuration(run.duration_ms)}</span>
      </div>
      {run.last_error !== null && <div className="auto-rerr">{run.last_error}</div>}
    </>
  );
}

interface CardProps {
  auto: Automation;
  open: boolean;
  running: boolean;
  onToggleOpen: () => void;
  onFlip: () => void;
  onRun: () => void;
  onAllRuns: () => void;
}

function AutomationCard({
  auto,
  open,
  running,
  onToggleOpen,
  onFlip,
  onRun,
  onAllRuns,
}: CardProps) {
  const dot = auto.enabled ? statusDotClass(auto.recent_runs[0]?.status ?? "done") : "idle";
  const stepWord = auto.steps.length === 1 ? "step" : "steps";
  return (
    <div className={`auto-card${auto.enabled ? "" : " off"}${open ? " open" : ""}`}>
      <div className="auto-head">
        <button type="button" className="auto-headbtn" onClick={onToggleOpen}>
          <span className={`auto-sdot ${running ? "running" : dot}`} aria-hidden="true" />
          <span className="auto-main">
            <span className="auto-when">{whenLine(auto)}</span>
            <span className="auto-meta">{metaLine(auto)}</span>
          </span>
          <ChevronRightIcon size={15} />
        </button>
        <button
          type="button"
          role="switch"
          aria-checked={auto.enabled}
          aria-label={`${auto.enabled ? "Disable" : "Enable"} ${auto.pipeline}`}
          className="auto-sw"
          onClick={onFlip}
        >
          <span className="auto-knob" />
        </button>
      </div>
      {open && (
        <div className="auto-body">
          <div className="auto-blab">
            Pipeline · {auto.steps.length} {stepWord}
          </div>
          <div className="auto-steps">
            {auto.steps.map((s, i) => (
              <StepRow key={s.action} step={s} last={i === auto.steps.length - 1} />
            ))}
          </div>
          <div className="auto-blab">Recent runs</div>
          {auto.recent_runs.length === 0 ? (
            <p className="muted auto-norun">No runs yet.</p>
          ) : (
            auto.recent_runs.map((r) => <RunRow key={r.id} run={r} />)
          )}
          <div className="auto-acts">
            <button type="button" onClick={onAllRuns}>
              <ListIcon size={14} />
              All runs
            </button>
            <button
              type="button"
              className={`auto-runbtn${running ? " running" : ""}`}
              disabled={!auto.manual || !auto.enabled || running}
              title={auto.manual ? undefined : "event-driven — fires automatically"}
              onClick={onRun}
            >
              {running ? <RefreshIcon size={14} /> : <PlayIcon size={14} />}
              {running ? "queued…" : "Run now"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function CatalogRow({ action }: { action: CatalogAction }) {
  return (
    <div className="auto-catrow">
      <span className="auto-cic">
        <ZapIcon size={16} />
      </span>
      <span className="auto-catmain">
        <span className="auto-cn">{action.name}</span>
        {action.description && <span className="auto-cd">{action.description}</span>}
        <span className="auto-cmeta">
          <span className={`auto-pill auto-pill-${action.cost_class}`}>{action.cost_class}</span>
          <span className="auto-pill auto-pill-dom">
            {action.domain_optional ? "cross-domain" : "scoped"}
          </span>
          <span className="auto-pill auto-pill-opt">
            {action.mutating ? "mutating" : "read-only"}
          </span>
          <span className="auto-pill auto-pill-opt">{action.seeded ? "seeded" : "in-code"}</span>
        </span>
      </span>
    </div>
  );
}

interface AutomationsScreenProps {
  onClose: () => void;
  /** Drill-through to the Runs surface where the mock links "All runs". */
  onOpenRuns: () => void;
}

type Tab = "auto" | "cat";

export function AutomationsScreen({ onClose, onOpenRuns }: AutomationsScreenProps) {
  const [automations, setAutomations] = useState<Automation[] | null>(null);
  const [actions, setActions] = useState<CatalogAction[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("auto");
  const [openId, setOpenId] = useState<string | null>(null);
  const [running, setRunning] = useState<Set<string>>(new Set());
  const [toast, setToast] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const data = await api.automations();
      setAutomations(data.automations);
      setActions(data.actions);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Optimistic toggle: flip locally, persist; on failure revert + surface it.
  // Schedule-bound triggers toggle the schedule too, so a disabled schedule stops
  // the tick AND the trigger — the operator's single switch governs both.
  async function flip(auto: Automation) {
    const next = !auto.enabled;
    setAutomations(
      (list) =>
        list?.map((a) => (a.trigger_id === auto.trigger_id ? { ...a, enabled: next } : a)) ?? null,
    );
    try {
      await api.setTriggerEnabled(auto.trigger_id, next);
      if (auto.schedule_id !== null) {
        await api.setScheduleEnabled(auto.schedule_id, next);
      }
      setToast(`${auto.pipeline} ${next ? "enabled" : "disabled"}.`);
    } catch (err) {
      setAutomations(
        (list) =>
          list?.map((a) =>
            a.trigger_id === auto.trigger_id ? { ...a, enabled: auto.enabled } : a,
          ) ?? null,
      );
      setToast(errorMessage(err));
    }
  }

  async function runNow(auto: Automation) {
    if (!auto.manual || !auto.enabled) return;
    setRunning((s) => new Set(s).add(auto.trigger_id));
    try {
      await api.runTrigger(auto.trigger_id);
      setToast(`Fired ${auto.pipeline} — run queued.`);
      await refresh();
    } catch (err) {
      setToast(errorMessage(err));
    } finally {
      setRunning((s) => {
        const next = new Set(s);
        next.delete(auto.trigger_id);
        return next;
      });
    }
  }

  return (
    <section className="runs-screen">
      <header className="runs-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back to Ops">
          <ChevronLeftIcon size={22} />
        </button>
        <h2 className="runs-bar-title">Workflow</h2>
        <button
          type="button"
          className="icon-btn runs-refresh"
          onClick={refresh}
          aria-label="Refresh"
        >
          <RefreshIcon size={20} />
        </button>
      </header>

      <div className="auto-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "auto"}
          className="auto-tab"
          onClick={() => setTab("auto")}
        >
          <ZapIcon size={15} />
          Automations
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "cat"}
          className="auto-tab"
          onClick={() => setTab("cat")}
        >
          <ListIcon size={15} />
          Catalog
        </button>
      </div>

      <div className="runs-body">
        {error !== null && (
          <p className="error" role="alert">
            {error}
          </p>
        )}

        {tab === "auto" ? (
          automations === null && error === null ? (
            <p className="muted">Loading automations…</p>
          ) : automations !== null && automations.length === 0 ? (
            <p className="muted runs-empty">No automations configured.</p>
          ) : (
            GROUPS.map(({ key, label }) => {
              const inGroup = automations?.filter((a) => a.group === key) ?? [];
              if (inGroup.length === 0) return null;
              return (
                <div key={key}>
                  <h3 className="runs-sect">{label}</h3>
                  {inGroup.map((auto) => (
                    <AutomationCard
                      key={auto.trigger_id}
                      auto={auto}
                      open={openId === auto.trigger_id}
                      running={running.has(auto.trigger_id)}
                      onToggleOpen={() =>
                        setOpenId((id) => (id === auto.trigger_id ? null : auto.trigger_id))
                      }
                      onFlip={() => void flip(auto)}
                      onRun={() => void runNow(auto)}
                      onAllRuns={onOpenRuns}
                    />
                  ))}
                </div>
              );
            })
          )
        ) : (
          <>
            <h3 className="runs-sect">Action registry · {actions.length} actions</h3>
            <div className="auto-cat">
              {actions.map((action) => (
                <CatalogRow key={action.name} action={action} />
              ))}
            </div>
            <p className="muted auto-cathint">
              Data-defined handlers the engine wires into a pipeline. "seeded" actions are mirrored
              into app.actions; the rest live in-code only.
            </p>
          </>
        )}
      </div>

      {toast !== null && (
        <output className="runs-toast">
          <CheckIcon size={16} />
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
