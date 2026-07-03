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
  type ScheduleFreq,
  type ScheduleInput,
  type ScheduleSpecKind,
  api,
} from "../api/client";
import {
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ListIcon,
  PencilIcon,
  PlayIcon,
  RefreshIcon,
  XIcon,
  ZapIcon,
} from "../components/icons";

// Sunday=0 … Saturday=6 — the chip row + the spec's day convention (matches Tasks).
const DAY_LABELS = ["S", "M", "T", "W", "T", "F", "S"];
const DAY_KEYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

/** The three sections, in order, bucketed by subject (not by how a trigger fires):
 * the note lifecycle up top, then the wiki, then background maintenance. An
 * automation's `group` (from its action's category) picks its section. */
const GROUPS: { key: Automation["group"]; label: string }[] = [
  { key: "note", label: "Notes" },
  { key: "wiki", label: "Wiki" },
  { key: "maintenance", label: "Maintenance" },
];

/** Sections collapsed on first render — Maintenance is background hygiene the owner
 * rarely touches, so it ships folded while Notes and Wiki stay open. */
const DEFAULT_COLLAPSED: Automation["group"][] = ["maintenance"];

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

/** "07:00" → "7:00" (drop the hour's leading zero for the headline). */
function fmtClock(hhmm: string): string {
  const [h, m] = hhmm.split(":");
  return `${Number(h)}:${m ?? "00"}`;
}

/** A schedule-bound automation's cadence phrase. The task-style spec kinds read like
 * a task's schedule label; `interval` (the legacy reconciler cadence) falls back to
 * the fixed-interval phrasing. */
function schedulePhrase(a: Automation): string {
  switch (a.schedule_kind) {
    case "repeat": {
      const time = a.schedule_time ? ` · ${fmtClock(a.schedule_time)}` : "";
      if (a.schedule_freq === "weekly") {
        const days = a.schedule_days.map((d) => DAY_LABELS[d]).join("");
        return `Weekly ${days}${time}`;
      }
      return `${a.schedule_freq === "weekdays" ? "Weekdays" : "Daily"}${time}`;
    }
    case "once":
      return a.run_at
        ? `Once · ${new Date(a.run_at).toLocaleString([], {
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          })}`
        : "Once";
    case "on_demand":
      return "On demand";
    default:
      return a.interval_seconds !== null ? fmtInterval(a.interval_seconds) : "scheduled";
  }
}

/** The when -> do headline. Event: "When <ev> → run <pipeline>"; schedule:
 * "<cadence> → run <pipeline>". */
function whenLine(a: Automation) {
  if (a.kind === "on_event") {
    return (
      <>
        When <span className="auto-ev">{a.on_event}</span> → run{" "}
        <span className="auto-pl">{a.pipeline}</span>
      </>
    );
  }
  const phrase = schedulePhrase(a);
  const cap = phrase.charAt(0).toUpperCase() + phrase.slice(1);
  return (
    <>
      {cap} → run <span className="auto-pl">{a.pipeline}</span>
    </>
  );
}

/** A sub-day sweep (the reconciler/geofence/inbox cadence): edited with the
 * number+unit interval control, never the wall-clock day/time picker that would
 * downgrade it to once a day. Read off the live cadence, not the display group —
 * the note/maintenance sections now each mix interval sweeps and daily ones. */
function isIntervalCadence(a: Automation): boolean {
  return a.interval_seconds !== null && a.interval_seconds <= 3600;
}

/** Whether a card offers a schedule editor. Every schedule-bound sweep is editable
 * (interval or day/time, per its cadence); event triggers are not scheduled. */
function isEditableSchedule(a: Automation): boolean {
  return a.kind === "schedule" && a.schedule_id !== null;
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

// --- the schedule editor (the task-style day/time/repeat surface, reused) --------

/** The editor's local draft — the schedule-spec subset of a task's Draft, plus an
 * `interval` kind for the reconciler's sub-day cadence. A nightly sweep's `interval`/
 * legacy state is mapped onto `repeat` so opening a never-edited one lands on a sensible
 * day/time; a reconciler instead opens on its number+unit `interval` cadence. */
interface SchedDraft {
  kind: "on_demand" | "once" | "repeat" | "interval";
  freq: ScheduleFreq;
  days: number[];
  time: string;
  date: string;
  /** Sub-day cadence for `interval` mode — a positive integer count of `intervalUnit`. */
  intervalValue: number;
  intervalUnit: "min" | "hr";
}

function tomorrowISODate(): string {
  const d = new Date(Date.now() + 86400000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** The next-fire instant as a local HH:MM — the natural default time when promoting a
 * legacy interval sweep to a wall-clock repeat (a nightly 02:00 UTC sweep shows the
 * owner's local equivalent). */
function localClockOf(iso: string | null): string {
  if (iso === null) return "02:00";
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** A reconciler's seeded interval as a number+unit pair — prefer whole hours (>= 1h)
 * for a tidy "every 6 hours" over "every 360 minutes", else minutes. */
function intervalParts(seconds: number | null): { value: number; unit: "min" | "hr" } {
  if (seconds !== null && seconds >= 3600 && seconds % 3600 === 0) {
    return { value: seconds / 3600, unit: "hr" };
  }
  return { value: seconds !== null ? Math.max(1, Math.round(seconds / 60)) : 5, unit: "min" };
}

function draftFromAuto(a: Automation): SchedDraft {
  const { value: intervalValue, unit: intervalUnit } = intervalParts(a.interval_seconds);
  const base: SchedDraft = {
    kind: "repeat",
    freq: "daily",
    days: [1, 2, 3, 4, 5],
    time: localClockOf(a.next_run_at),
    date: tomorrowISODate(),
    intervalValue,
    intervalUnit,
  };
  // A sub-day sweep edits its cadence: open on Interval (or On demand if paused),
  // never the wall-clock repeat that would downgrade a minute-level sweep to once a day.
  if (isIntervalCadence(a)) {
    return { ...base, kind: a.schedule_kind === "on_demand" ? "on_demand" : "interval" };
  }
  if (a.schedule_kind === "repeat") {
    return {
      ...base,
      kind: "repeat",
      freq: a.schedule_freq ?? "daily",
      days: a.schedule_days.length ? a.schedule_days : [1, 2, 3, 4, 5],
      time: a.schedule_time ?? base.time,
    };
  }
  if (a.schedule_kind === "once") {
    return {
      ...base,
      kind: "once",
      time: a.run_at ? localClockOf(a.run_at) : base.time,
      date: a.run_at ? a.run_at.slice(0, 10) : tomorrowISODate(),
    };
  }
  if (a.schedule_kind === "on_demand") return { ...base, kind: "on_demand" };
  // interval (legacy) or null: default to a daily repeat at the current fire time.
  return base;
}

function intervalSeconds(d: SchedDraft): number {
  return d.intervalValue * (d.intervalUnit === "hr" ? 3600 : 60);
}

function draftToScheduleInput(d: SchedDraft): ScheduleInput {
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const base: ScheduleInput = {
    schedule_kind: d.kind as ScheduleSpecKind,
    interval_seconds: null,
    schedule_freq: null,
    schedule_days: [],
    schedule_time: null,
    run_at: null,
    timezone,
  };
  if (d.kind === "interval") {
    base.interval_seconds = intervalSeconds(d);
  } else if (d.kind === "repeat") {
    base.schedule_freq = d.freq;
    base.schedule_time = d.time;
    base.schedule_days = d.freq === "weekly" ? d.days : [];
  } else if (d.kind === "once") {
    base.run_at = new Date(`${d.date}T${d.time}`).toISOString();
  }
  return base;
}

interface ScheduleEditorProps {
  auto: Automation;
  saving: boolean;
  onClose: () => void;
  onSave: (input: ScheduleInput) => void;
}

/** The reconciler's quick-pick chips — minute counts; hour multiples render as a tidy
 * "every N hours". Mirrors the binding mock (5 min / 15 min / 30 min / Hourly / 6 h). */
const INTERVAL_CHIPS: { mins: number; label: string }[] = [
  { mins: 5, label: "5 min" },
  { mins: 15, label: "15 min" },
  { mins: 30, label: "30 min" },
  { mins: 60, label: "Hourly" },
  { mins: 360, label: "6 h" },
];

/** Whether the draft's interval is a positive integer — the only saveable interval. */
function validInterval(d: SchedDraft): boolean {
  return Number.isInteger(d.intervalValue) && d.intervalValue >= 1;
}

function ScheduleEditor({ auto, saving, onClose, onSave }: ScheduleEditorProps) {
  const [draft, setDraft] = useState<SchedDraft>(() => draftFromAuto(auto));
  const set = (patch: Partial<SchedDraft>) => setDraft((d) => ({ ...d, ...patch }));
  function toggleDay(day: number): void {
    setDraft((d) => ({
      ...d,
      days: d.days.includes(day) ? d.days.filter((x) => x !== day) : [...d.days, day].sort(),
    }));
  }
  // Sub-day sweeps edit a fixed interval; everything else uses the task-style day/time editor.
  const isReconcile = isIntervalCadence(auto);
  const modes: SchedDraft["kind"][] = isReconcile
    ? ["interval", "on_demand"]
    : ["on_demand", "once", "repeat"];
  const intervalOk = validInterval(draft);
  const canSave =
    !saving &&
    !(draft.kind === "repeat" && draft.freq === "weekly" && !draft.days.length) &&
    !(draft.kind === "interval" && !intervalOk);

  // A chip selects a minute count, mapping whole-hour multiples onto the Hours unit.
  function pickChip(mins: number): void {
    if (mins % 60 === 0) set({ intervalValue: mins / 60, intervalUnit: "hr" });
    else set({ intervalValue: mins, intervalUnit: "min" });
  }
  const chipMins = draft.intervalUnit === "hr" ? draft.intervalValue * 60 : draft.intervalValue;

  return (
    <section className="ted">
      <header className="runs-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Cancel">
          <ChevronLeftIcon size={22} />
        </button>
        <h2 className="runs-bar-title">Edit schedule</h2>
      </header>
      <div className="ted-body">
        <div className="ted-field">
          <span className="ted-lab">
            {isReconcile ? "How it runs" : "When it runs"} · {auto.pipeline}
          </span>
          <div className="ted-seg" role="tablist">
            {modes.map((k) => (
              <button
                type="button"
                key={k}
                role="tab"
                aria-selected={draft.kind === k}
                className={`ted-segbtn${draft.kind === k ? " on" : ""}`}
                onClick={() => set({ kind: k })}
              >
                {k === "on_demand"
                  ? "On demand"
                  : k === "once"
                    ? "Once"
                    : k === "interval"
                      ? "Interval"
                      : "Repeats"}
              </button>
            ))}
          </div>
          {draft.kind === "on_demand" && (
            <p className="ted-hint">
              {isReconcile
                ? "Paused — it only runs when you tap Run now."
                : "No schedule — fire it yourself with Run now."}
            </p>
          )}
          {draft.kind === "interval" && (
            <>
              <p className="ted-hint">
                A reconciler runs on a fixed cadence — set the gap between sweeps.
              </p>
              <span className="ted-lab ted-ivlab">Run every</span>
              <div className="ted-ivrow">
                <span className="ted-ivevery">Every</span>
                <input
                  className="ted-ivnum"
                  type="number"
                  min={1}
                  max={999}
                  inputMode="numeric"
                  aria-label="Interval value"
                  value={Number.isNaN(draft.intervalValue) ? "" : draft.intervalValue}
                  onChange={(e) => set({ intervalValue: e.target.valueAsNumber })}
                />
                <select
                  className="ted-ivunit"
                  aria-label="Interval unit"
                  value={draft.intervalUnit}
                  onChange={(e) => set({ intervalUnit: e.target.value as "min" | "hr" })}
                >
                  <option value="min">minutes</option>
                  <option value="hr">hours</option>
                </select>
              </div>
              <div className="ted-ivchips">
                {INTERVAL_CHIPS.map((c) => (
                  <button
                    type="button"
                    key={c.mins}
                    className={`ted-ivchip${chipMins === c.mins ? " on" : ""}`}
                    aria-pressed={chipMins === c.mins}
                    onClick={() => pickChip(c.mins)}
                  >
                    {c.label}
                  </button>
                ))}
              </div>
              {!intervalOk ? (
                <p className="ted-iverr">Enter a whole number of 1 or more.</p>
              ) : (
                <p className="ted-ivpreview">
                  Sweeps <b>{fmtInterval(intervalSeconds(draft))}</b>
                </p>
              )}
              {intervalOk && intervalSeconds(draft) >= 3600 && (
                <p className="ted-ivguard">
                  <b>Heads up:</b> an hour or longer between sweeps means new items wait that long
                  to be processed.
                </p>
              )}
            </>
          )}
          {draft.kind === "once" && (
            <div className="ted-row2">
              <input
                className="ted-input"
                type="date"
                value={draft.date}
                onChange={(e) => set({ date: e.target.value })}
              />
              <input
                className="ted-input"
                type="time"
                value={draft.time}
                onChange={(e) => set({ time: e.target.value })}
              />
            </div>
          )}
          {draft.kind === "repeat" && (
            <>
              <div className="ted-seg" style={{ marginTop: 10 }}>
                {(["daily", "weekdays", "weekly"] as ScheduleFreq[]).map((f) => (
                  <button
                    type="button"
                    key={f}
                    className={`ted-segbtn${draft.freq === f ? " on" : ""}`}
                    aria-pressed={draft.freq === f}
                    onClick={() => set({ freq: f })}
                  >
                    {f === "daily" ? "Daily" : f === "weekdays" ? "Weekdays" : "Weekly"}
                  </button>
                ))}
              </div>
              {draft.freq === "weekly" && (
                <div className="ted-days">
                  {DAY_KEYS.map((dayKey, i) => (
                    <button
                      type="button"
                      key={dayKey}
                      className={`ted-day${draft.days.includes(i) ? " on" : ""}`}
                      aria-pressed={draft.days.includes(i)}
                      aria-label={dayKey}
                      onClick={() => toggleDay(i)}
                    >
                      {DAY_LABELS[i]}
                    </button>
                  ))}
                </div>
              )}
              <input
                className="ted-input"
                style={{ marginTop: 10 }}
                type="time"
                value={draft.time}
                onChange={(e) => set({ time: e.target.value })}
              />
            </>
          )}
        </div>
      </div>
      <div className="ted-foot">
        <button
          type="button"
          className="ted-save"
          disabled={!canSave}
          onClick={() => onSave(draftToScheduleInput(draft))}
        >
          {saving ? "Saving…" : "Save schedule"}
        </button>
      </div>
    </section>
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
  onEdit: () => void;
}

function AutomationCard({
  auto,
  open,
  running,
  onToggleOpen,
  onFlip,
  onRun,
  onAllRuns,
  onEdit,
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
            {isEditableSchedule(auto) && (
              <button type="button" onClick={onEdit}>
                <PencilIcon size={14} />
                Edit schedule
              </button>
            )}
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
  const [editing, setEditing] = useState<Automation | null>(null);
  const [savingSchedule, setSavingSchedule] = useState(false);
  // Which sections are folded. Maintenance ships collapsed (background hygiene the
  // owner rarely opens); tapping a section header toggles it.
  const [collapsed, setCollapsed] = useState<Set<Automation["group"]>>(
    () => new Set(DEFAULT_COLLAPSED),
  );
  const toggleCollapsed = (key: Automation["group"]) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

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

  async function saveSchedule(auto: Automation, input: ScheduleInput) {
    if (auto.schedule_id === null) return;
    setSavingSchedule(true);
    try {
      await api.updateSchedule(auto.schedule_id, input);
      setEditing(null);
      setToast(`${auto.pipeline} schedule updated.`);
      await refresh();
    } catch (err) {
      setToast(errorMessage(err));
    } finally {
      setSavingSchedule(false);
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

  if (editing !== null) {
    return (
      <ScheduleEditor
        auto={editing}
        saving={savingSchedule}
        onClose={() => setEditing(null)}
        onSave={(input) => void saveSchedule(editing, input)}
      />
    );
  }

  return (
    <section className="runs-screen">
      <header className="runs-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back to launcher">
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
              const isCollapsed = collapsed.has(key);
              return (
                <div key={key}>
                  <button
                    type="button"
                    className="runs-sect auto-sect"
                    aria-expanded={!isCollapsed}
                    onClick={() => toggleCollapsed(key)}
                  >
                    <ChevronRightIcon size={13} />
                    {label}
                    <span className="auto-sect-count">{inGroup.length}</span>
                  </button>
                  {!isCollapsed &&
                    inGroup.map((auto) => (
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
                        onEdit={() => setEditing(auto)}
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
