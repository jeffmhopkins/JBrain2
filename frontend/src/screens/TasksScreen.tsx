// The Tasks surface — saved prompts that spawn an agent session on a schedule or on
// demand. Organization is GUI Direction B (docs/mocks/task-grouping/b-chips-move-sheet):
// grouping is a *filter* (a chip row switches buckets), filing a task is a deliberate
// ⋯ → Move-to sheet (never a drag across the screen), and an Organize toggle arms a
// lightweight reorder *within the current view* (drag the grip, or arrow-key it) plus
// inline rename / delete of the owner's groups. In the "All" view each category
// header folds its cards away — a device-local, persisted collapse (see tasks/collapsed).
// Cards still read "agent · schedule" with an enable toggle + next/last-run meta and
// the docked "latest result" band.
// Reflects live state — cards come from /api/tasks, buckets from /api/task-groups.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type ScheduleFreq,
  type ScheduleKind,
  type Task,
  type TaskAgent,
  type TaskGroup,
  type TaskInput,
  type TaskRun,
  api,
} from "../api/client";
import { useBackLayer } from "../backLayers";
import { MoveTaskSheet } from "../components/MoveTaskSheet";
import {
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  GripIcon,
  MoreIcon,
  PencilIcon,
  PlayIcon,
  PlusIcon,
  RefreshIcon,
  ReorderIcon,
  TrashIcon,
  XIcon,
} from "../components/icons";
import { UNGROUPED_KEY, loadCollapsed, writeCollapsed } from "../tasks/collapsed";
import { isUnviewed, loadViewed, writeViewed } from "../tasks/viewed";

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "Request failed. Is the server reachable?";
}

const AGENT_LABEL: Record<TaskAgent, string> = {
  jerv: "Jerv",
  curator: "Curator",
  teacher: "Teacher",
  archivist: "Archivist",
};
const DAY_LABELS = ["S", "M", "T", "W", "T", "F", "S"]; // Sunday=0 … Saturday=6
const DAY_KEYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]; // stable keys for the chips

/** The chip filter value: everything, one group, or the trailing ungrouped bucket. */
const ALL = "all";
const UNGROUPED = "ungrouped";

function fmtAgo(iso: string): string {
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function fmtNext(iso: string): string {
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return "due now";
  const m = Math.round(diff / 60000);
  if (m < 60) return `in ${Math.max(1, m)}m`;
  const h = Math.round(m / 60);
  if (h < 24) return `in ${h}h`;
  return `in ${Math.round(h / 24)}d`;
}

/** The schedule headline, e.g. "Weekdays · 7:00" or "Once · Jun 26, 9:00". */
function scheduleLabel(t: Task): string {
  if (t.schedule_kind === "on_demand") return "On demand";
  if (t.schedule_kind === "once") {
    if (!t.run_at) return "Once";
    const d = new Date(t.run_at);
    return `Once · ${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  }
  const time = t.schedule_time ?? "";
  if (t.schedule_freq === "daily") return `Daily · ${time}`;
  if (t.schedule_freq === "weekdays") return `Weekdays · ${time}`;
  const days = t.schedule_days.map((d) => DAY_LABELS[d]).join("");
  return `Weekly ${days} · ${time}`;
}

/** The dim status + next-run meta under the headline. */
function metaLine(t: Task): string {
  const parts: string[] = [];
  if (!t.enabled) parts.push("paused");
  else if (t.schedule_kind === "on_demand") parts.push("run it any time");
  else parts.push("scheduled");
  if (t.enabled && t.next_run_at) parts.push(`next ${fmtNext(t.next_run_at)}`);
  else if (t.last_run_at) parts.push(`last ran ${fmtAgo(t.last_run_at)}`);
  return parts.join(" · ");
}

function runDot(status: TaskRun["status"]): string {
  return status === "error" ? "failed" : status === "running" ? "running" : "ok";
}

/** The card's "latest result" band — a one-tap dock to the newest run's session,
 * shown only while that result is unviewed. Once its session has been opened on this
 * device the band disappears and the card collapses to its header; the full run
 * history still lives in the expanded body. A task that has never run shows an inert
 * "No runs yet" placeholder. */
function TaskBand({
  task,
  unviewed,
  onOpenRun,
}: {
  task: Task;
  unviewed: boolean;
  onOpenRun: (run: TaskRun) => void;
}) {
  const latest = task.latest_run;
  if (latest === null) {
    return (
      <div className="task-band empty">
        <span className="task-bd idle" aria-hidden="true" />
        <span className="task-bt">No runs yet</span>
      </div>
    );
  }
  if (!unviewed) return null;
  const text =
    latest.status === "error" ? (latest.error ?? "failed") : latest.summary || "(no output)";
  const dot =
    latest.status === "error" ? "failed" : latest.status === "running" ? "running" : "new";
  const body = (
    <>
      <span className={`task-bd ${dot}`} aria-hidden="true" />
      <span className="task-bt">
        <span className="task-band-new">NEW</span>
        {text}
      </span>
      <span className="task-bm">
        {latest.step_count > 0
          ? `${latest.step_count} turn${latest.step_count === 1 ? "" : "s"} · `
          : ""}
        {fmtAgo(latest.started_at)}
      </span>
    </>
  );
  if (latest.session_id === null) {
    return <div className="task-band inert unviewed">{body}</div>;
  }
  return (
    <button
      type="button"
      className="task-band unviewed"
      onClick={() => onOpenRun(latest)}
      aria-label={`Open latest session: ${text}`}
    >
      {body}
      <ChevronRightIcon size={15} />
    </button>
  );
}

interface CardProps {
  task: Task;
  open: boolean;
  running: boolean;
  reordering: boolean;
  unviewed: boolean;
  runs: TaskRun[] | null;
  onToggleOpen: () => void;
  onFlip: () => void;
  onRun: () => void;
  onEdit: () => void;
  onMove: () => void;
  onOpenRun: (run: TaskRun) => void;
  onDragStart: (e: React.PointerEvent) => void;
  onNudge: (dir: -1 | 1) => void;
}

function TaskCard({
  task,
  open,
  running,
  reordering,
  unviewed,
  runs,
  onToggleOpen,
  onFlip,
  onRun,
  onEdit,
  onMove,
  onOpenRun,
  onDragStart,
  onNudge,
}: CardProps) {
  const dot = running ? "running" : !task.enabled ? "idle" : "ok";
  return (
    <div
      className={`task-card${task.enabled ? "" : " off"}${open ? " open" : ""}${reordering ? " reordering" : ""}`}
      data-task-id={task.id}
    >
      <div className="task-head">
        {reordering && (
          <button
            type="button"
            className="task-grab"
            aria-label={`Reorder ${task.name || "task"} (drag, or use the arrow keys)`}
            onPointerDown={onDragStart}
            onKeyDown={(e) => {
              if (e.key === "ArrowUp") {
                e.preventDefault();
                onNudge(-1);
              } else if (e.key === "ArrowDown") {
                e.preventDefault();
                onNudge(1);
              }
            }}
          >
            <GripIcon size={18} />
          </button>
        )}
        <button type="button" className="task-headbtn" onClick={onToggleOpen} disabled={reordering}>
          <span className={`task-dot ${dot}`} aria-hidden="true" />
          <span className="task-main">
            <span className="task-name">{task.name || "Untitled task"}</span>
            <span className="task-when">
              <span className={`task-ag ${task.agent}`}>
                {AGENT_LABEL[task.agent]}
                {task.agent === "curator" && task.domain_scopes.length > 0
                  ? ` · ${task.domain_scopes.length === 4 ? "everything" : task.domain_scopes.join(", ")}`
                  : ""}
              </span>
              <span>{scheduleLabel(task)}</span>
            </span>
            <span className="task-meta">{metaLine(task)}</span>
          </span>
          {!reordering && <ChevronRightIcon size={15} />}
        </button>
        {!reordering && (
          <>
            <button
              type="button"
              role="switch"
              aria-checked={task.enabled}
              aria-label={`${task.enabled ? "Pause" : "Resume"} ${task.name || "task"}`}
              className="task-sw"
              onClick={onFlip}
            >
              <span className="task-knob" />
            </button>
            <button
              type="button"
              className="task-kebab"
              aria-label={`Move ${task.name || "task"} to a group`}
              onClick={onMove}
            >
              <MoreIcon size={18} />
            </button>
          </>
        )}
      </div>
      {open && !reordering && (
        <div className="task-body">
          <div className="task-blab">Prompt</div>
          <div className="task-prompt">{task.prompt}</div>
          <div className="task-blab">Recent runs → sessions</div>
          {runs === null ? (
            <p className="muted task-norun">Loading runs…</p>
          ) : runs.length === 0 ? (
            <p className="muted task-norun">No runs yet.</p>
          ) : (
            runs.map((r) =>
              r.session_id ? (
                <button
                  type="button"
                  key={r.id}
                  className="task-run task-run-link"
                  onClick={() => onOpenRun(r)}
                  aria-label={`Open session: ${r.summary || r.status}`}
                >
                  <span className={`task-rd ${runDot(r.status)}`} aria-hidden="true" />
                  <span className="task-rt">
                    {r.status === "error" ? (r.error ?? "failed") : r.summary || "(no output)"}
                  </span>
                  <span className="task-rm">
                    {r.step_count > 0
                      ? `${r.step_count} turn${r.step_count === 1 ? "" : "s"} · `
                      : ""}
                    {fmtAgo(r.started_at)}
                  </span>
                  <ChevronRightIcon size={14} />
                </button>
              ) : (
                <div key={r.id} className="task-run">
                  <span className={`task-rd ${runDot(r.status)}`} aria-hidden="true" />
                  <span className="task-rt">
                    {r.status === "error" ? (r.error ?? "failed") : r.summary || "(no output)"}
                  </span>
                  <span className="task-rm">
                    {r.step_count > 0
                      ? `${r.step_count} turn${r.step_count === 1 ? "" : "s"} · `
                      : ""}
                    {fmtAgo(r.started_at)}
                  </span>
                </div>
              ),
            )
          )}
          <div className="task-acts">
            <button type="button" onClick={onEdit}>
              <PencilIcon size={14} />
              Edit
            </button>
            <button
              type="button"
              className={`task-runbtn${running ? " running" : ""}`}
              disabled={running || !task.enabled}
              onClick={onRun}
            >
              {running ? <RefreshIcon size={14} /> : <PlayIcon size={14} />}
              {running ? "running…" : "Run now"}
            </button>
          </div>
        </div>
      )}
      <TaskBand task={task} unviewed={unviewed} onOpenRun={onOpenRun} />
    </div>
  );
}

// ---- the editor ----

const SCOPE_PRESETS: { id: string; label: string; set: string[] | null }[] = [
  { id: "everything", label: "Everything", set: ["general", "health", "finance", "location"] },
  { id: "general", label: "General", set: ["general"] },
  { id: "medical", label: "Medical", set: ["general", "health"] },
  { id: "financial", label: "Financial", set: ["general", "finance"] },
  { id: "custom", label: "Custom…", set: null },
];
const DOMAINS: { code: string; label: string }[] = [
  { code: "general", label: "General" },
  { code: "health", label: "Medical" },
  { code: "finance", label: "Financial" },
  { code: "location", label: "Location" },
];

interface Draft {
  id: string | null;
  name: string;
  prompt: string;
  agent: TaskAgent;
  scopes: string[];
  kind: ScheduleKind;
  freq: ScheduleFreq;
  days: number[];
  time: string;
  date: string;
  notifyPush: boolean;
  homeCard: boolean;
  enabled: boolean;
}

function tomorrowISODate(): string {
  const d = new Date(Date.now() + 86400000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function draftFrom(task: Task | null): Draft {
  if (task === null) {
    return {
      id: null,
      name: "",
      prompt: "",
      agent: "jerv",
      scopes: ["general", "health", "finance", "location"],
      kind: "repeat",
      freq: "weekdays",
      days: [1, 2, 3, 4, 5],
      time: "07:00",
      date: tomorrowISODate(),
      notifyPush: true,
      homeCard: true,
      enabled: true,
    };
  }
  return {
    id: task.id,
    name: task.name,
    prompt: task.prompt,
    agent: task.agent,
    scopes: task.domain_scopes.length ? task.domain_scopes : ["general"],
    kind: task.schedule_kind,
    freq: task.schedule_freq ?? "weekdays",
    days: task.schedule_days.length ? task.schedule_days : [1, 2, 3, 4, 5],
    time: task.schedule_time ?? "07:00",
    date: task.run_at ? task.run_at.slice(0, 10) : tomorrowISODate(),
    notifyPush: task.notify_push,
    homeCard: task.home_card,
    enabled: task.enabled,
  };
}

function presetForScopes(scopes: string[]): string {
  for (const p of SCOPE_PRESETS) {
    if (p.set && p.set.length === scopes.length && p.set.every((c) => scopes.includes(c))) {
      return p.id;
    }
  }
  return "custom";
}

function draftToInput(d: Draft): TaskInput {
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const base: TaskInput = {
    name: d.name.trim(),
    prompt: d.prompt.trim(),
    agent: d.agent,
    domain_scopes: d.agent === "curator" ? d.scopes : [],
    schedule_kind: d.kind,
    schedule_freq: null,
    schedule_days: [],
    schedule_time: null,
    run_at: null,
    timezone,
    enabled: d.enabled,
    notify_push: d.notifyPush,
    home_card: d.homeCard,
  };
  if (d.kind === "repeat") {
    base.schedule_freq = d.freq;
    base.schedule_time = d.time;
    base.schedule_days = d.freq === "weekly" ? d.days : [];
  } else if (d.kind === "once") {
    // Combine the local date + time into an absolute instant.
    base.run_at = new Date(`${d.date}T${d.time}`).toISOString();
  }
  return base;
}

interface EditorProps {
  draft: Draft;
  onChange: (d: Draft) => void;
  onClose: () => void;
  onSave: () => void;
  saving: boolean;
}

function Editor({ draft, onChange, onClose, onSave, saving }: EditorProps) {
  // The editor is a full-screen layer over the task list; the back gesture closes it
  // back to the list (not the whole Tasks card). It mounts only while editing, so the
  // registration tracks its own visibility — like the shared Sheet.
  useBackLayer(onClose);
  const set = (patch: Partial<Draft>) => onChange({ ...draft, ...patch });
  const preset = presetForScopes(draft.scopes);
  const canSave = draft.prompt.trim().length > 0 && !saving;

  function pickScope(id: string): void {
    const found = SCOPE_PRESETS.find((p) => p.id === id);
    if (found?.set) set({ scopes: found.set });
    else set({ scopes: draft.scopes.length ? draft.scopes : ["general"] }); // custom: keep current
  }
  function toggleDomain(code: string): void {
    const has = draft.scopes.includes(code);
    set({ scopes: has ? draft.scopes.filter((c) => c !== code) : [...draft.scopes, code] });
  }
  function toggleDay(day: number): void {
    const has = draft.days.includes(day);
    set({ days: has ? draft.days.filter((d) => d !== day) : [...draft.days, day].sort() });
  }

  return (
    <section className="ted">
      <header className="runs-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Cancel">
          <ChevronLeftIcon size={22} />
        </button>
        <h2 className="runs-bar-title">{draft.id ? "Edit task" : "New task"}</h2>
      </header>
      <div className="ted-body">
        <label className="ted-field">
          <span className="ted-lab">Name</span>
          <input
            className="ted-input"
            value={draft.name}
            placeholder="e.g. Morning news brief"
            onChange={(e) => set({ name: e.target.value })}
          />
        </label>

        <label className="ted-field">
          <span className="ted-lab">Prompt</span>
          <textarea
            className="ted-input ted-area"
            value={draft.prompt}
            placeholder="Tell the agent what to do on each run…"
            onChange={(e) => set({ prompt: e.target.value })}
          />
        </label>

        <div className="ted-field">
          <span className="ted-lab">Agent</span>
          <div className="ted-agents">
            {(["jerv", "curator", "teacher", "archivist"] as TaskAgent[]).map((a) => (
              <button
                type="button"
                key={a}
                className={`ted-agent ${a}${draft.agent === a ? " on" : ""}`}
                aria-pressed={draft.agent === a}
                onClick={() => set({ agent: a })}
              >
                <span className="ted-av">{AGENT_LABEL[a][0]}</span>
                <span className="ted-am">
                  <span className="ted-at">{AGENT_LABEL[a]}</span>
                  <span className="ted-ad">
                    {a === "jerv"
                      ? "Web chatbot — reads the open internet, not your notes."
                      : a === "curator"
                        ? "Your Full Brain — reads your notes, facts, lists."
                        : a === "archivist"
                          ? "Organizes your Gmail — labels and archives, never deletes."
                          : "A Socratic tutor — works from the prompt only."}
                  </span>
                </span>
                <span className="ted-ck" aria-hidden="true">
                  <CheckIcon size={12} />
                </span>
              </button>
            ))}
          </div>
          {draft.agent === "curator" && (
            <div className="ted-scope">
              <span className="ted-lab">Reads</span>
              <div className="ted-presets">
                {SCOPE_PRESETS.map((p) => (
                  <button
                    type="button"
                    key={p.id}
                    className={`ted-preset${preset === p.id ? " on" : ""}`}
                    aria-pressed={preset === p.id}
                    onClick={() => pickScope(p.id)}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
              {preset === "custom" && (
                <div className="ted-grid">
                  {DOMAINS.map((d) => (
                    <button
                      type="button"
                      key={d.code}
                      className={`ted-dom${draft.scopes.includes(d.code) ? " on" : ""}`}
                      aria-pressed={draft.scopes.includes(d.code)}
                      onClick={() => toggleDomain(d.code)}
                    >
                      {d.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="ted-field">
          <span className="ted-lab">Schedule</span>
          <div className="ted-seg" role="tablist">
            {(["on_demand", "once", "repeat"] as ScheduleKind[]).map((k) => (
              <button
                type="button"
                key={k}
                role="tab"
                aria-selected={draft.kind === k}
                className={`ted-segbtn${draft.kind === k ? " on" : ""}`}
                onClick={() => set({ kind: k })}
              >
                {k === "on_demand" ? "On demand" : k === "once" ? "Once" : "Repeats"}
              </button>
            ))}
          </div>
          {draft.kind === "on_demand" && (
            <p className="ted-hint">No schedule — run it yourself from the task list.</p>
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

        <div className="ted-field">
          <span className="ted-lab">When it finishes</span>
          <div className="ted-deliv">
            <DeliveryRow
              title="Push notification"
              desc="Ping me when a scheduled run finishes or errors."
              on={draft.notifyPush}
              onToggle={() => set({ notifyPush: !draft.notifyPush })}
            />
            <DeliveryRow
              title="Home feed card"
              desc="Drop the latest run's summary on my home screen."
              on={draft.homeCard}
              onToggle={() => set({ homeCard: !draft.homeCard })}
            />
            <div className="ted-delivrow fixed">
              <span className="ted-dm">
                <span className="ted-dt">Saved to history</span>
                <span className="ted-dd">Every run becomes a session you can open later.</span>
              </span>
              <span className="ted-lock">always on</span>
            </div>
          </div>
        </div>
      </div>
      <div className="ted-foot">
        <button type="button" className="ted-save" disabled={!canSave} onClick={onSave}>
          {saving ? "Saving…" : draft.id ? "Save changes" : "Save task"}
        </button>
      </div>
    </section>
  );
}

function DeliveryRow({
  title,
  desc,
  on,
  onToggle,
}: {
  title: string;
  desc: string;
  on: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="ted-delivrow">
      <span className="ted-dm">
        <span className="ted-dt">{title}</span>
        <span className="ted-dd">{desc}</span>
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={title}
        className="task-sw"
        onClick={onToggle}
      >
        <span className="task-knob" />
      </button>
    </div>
  );
}

// ---- the screen ----

interface TasksScreenProps {
  onClose: () => void;
  /** Open the agent session a run produced (hands off to Full Brain). */
  onOpenSession: (sessionId: string, agent: TaskAgent) => void;
}

export function TasksScreen({ onClose, onOpenSession }: TasksScreenProps) {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [groups, setGroups] = useState<TaskGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>(ALL);
  const [reordering, setReordering] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [running, setRunning] = useState<Set<string>>(new Set());
  const [runsByTask, setRunsByTask] = useState<Record<string, TaskRun[]>>({});
  const [toast, setToast] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [viewed, setViewed] = useState<Record<string, string>>(loadViewed);
  const [collapsed, setCollapsed] = useState<Set<string>>(loadCollapsed);
  const [moveFor, setMoveFor] = useState<Task | null>(null);
  const [renamingGroup, setRenamingGroup] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [armedDelete, setArmedDelete] = useState<string | null>(null);
  const dragId = useRef<string | null>(null);

  // Opening the newest run's session clears that task's "new" band on this device.
  // Persist OUTSIDE the state updater: opening a session unmounts this screen, and
  // React never runs a queued updater for an unmounting component — so a write inside
  // it would be lost and the band would re-show "new". Writing synchronously here lands
  // before the unmount.
  const markViewed = useCallback((task: Task) => {
    const latest = task.latest_run;
    if (latest === null) return;
    const startedAt = latest.started_at;
    setViewed((m) => ({ ...m, [task.id]: startedAt }));
    writeViewed(task.id, startedAt);
  }, []);

  function openRun(task: Task, run: TaskRun): void {
    if (run.session_id === null) return;
    if (task.latest_run !== null && run.id === task.latest_run.id) markViewed(task);
    onOpenSession(run.session_id, task.agent);
  }

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [t, g] = await Promise.all([api.tasks(), api.taskGroups()]);
      setTasks(t);
      setGroups(g);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const loadRuns = useCallback(async (taskId: string) => {
    try {
      const runs = await api.taskRuns(taskId);
      setRunsByTask((m) => ({ ...m, [taskId]: runs }));
    } catch {
      setRunsByTask((m) => ({ ...m, [taskId]: [] }));
    }
  }, []);

  function toggleOpen(taskId: string): void {
    setOpenId((id) => {
      const next = id === taskId ? null : taskId;
      if (next !== null && runsByTask[next] === undefined) void loadRuns(next);
      return next;
    });
  }

  // Optimistic enable/disable: flip locally, persist; revert + surface on failure.
  async function flip(task: Task): Promise<void> {
    const next = !task.enabled;
    setTasks((list) => list?.map((t) => (t.id === task.id ? { ...t, enabled: next } : t)) ?? null);
    try {
      const updated = await api.setTaskEnabled(task.id, next);
      setTasks((list) => list?.map((t) => (t.id === task.id ? updated : t)) ?? null);
    } catch (err) {
      setTasks(
        (list) =>
          list?.map((t) => (t.id === task.id ? { ...t, enabled: task.enabled } : t)) ?? null,
      );
      setToast(errorMessage(err));
    }
  }

  async function runNow(task: Task): Promise<void> {
    setRunning((s) => new Set(s).add(task.id));
    try {
      await api.runTask(task.id);
      setToast(`Ran “${task.name || "task"}” — see recent runs.`);
      await loadRuns(task.id);
      await refresh();
    } catch (err) {
      setToast(errorMessage(err));
    } finally {
      setRunning((s) => {
        const n = new Set(s);
        n.delete(task.id);
        return n;
      });
    }
  }

  async function remove(task: Task): Promise<void> {
    try {
      await api.deleteTask(task.id);
      setTasks((list) => list?.filter((t) => t.id !== task.id) ?? null);
      setToast(`Deleted “${task.name || "task"}”.`);
    } catch (err) {
      setToast(errorMessage(err));
    }
  }

  async function save(): Promise<void> {
    if (draft === null) return;
    setSaving(true);
    try {
      const body = draftToInput(draft);
      if (draft.id) await api.replaceTask(draft.id, body);
      else await api.createTask(body);
      setDraft(null);
      await refresh();
      setToast(draft.id ? "Task saved." : "Task created.");
    } catch (err) {
      setToast(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  // ---- grouping + ordering ----

  const groupIdOf = (t: Task): string | null => t.group_id;
  const tasksInBucket = (bucket: string | null): Task[] =>
    (tasks ?? []).filter((t) => groupIdOf(t) === bucket);

  // Persist the current in-array order of a bucket as its authoritative order/positions.
  async function persistOrder(bucket: string | null, next: Task[]): Promise<void> {
    const ids = next.filter((t) => groupIdOf(t) === bucket).map((t) => t.id);
    try {
      await api.reorderTasks(bucket, ids);
    } catch (err) {
      setToast(errorMessage(err));
      await refresh();
    }
  }

  // Move `task` into `bucket` (null = Ungrouped), appended to that bucket's end.
  async function moveTo(task: Task, bucket: string | null): Promise<void> {
    setMoveFor(null);
    if (task.group_id === bucket) return;
    const next = (tasks ?? [])
      .filter((t) => t.id !== task.id)
      .concat([{ ...task, group_id: bucket }]);
    setTasks(next);
    const label = bucket === null ? "Ungrouped" : (groups.find((g) => g.id === bucket)?.name ?? "");
    setToast(`Moved to “${label}”.`);
    await persistOrder(bucket, next);
  }

  async function moveToNewGroup(task: Task, name: string): Promise<void> {
    setMoveFor(null);
    try {
      const g = await api.createTaskGroup(name);
      setGroups((gs) => [...gs, g]);
      await moveTo(task, g.id);
      // moveTo's own toast can't see the just-created group's name (stale closure).
      setToast(`Created “${g.name}” — task moved.`);
    } catch (err) {
      setToast(errorMessage(err));
      await refresh();
    }
  }

  // Reorder a task within its bucket by one step (the arrow-key path; drag uses the
  // same persist). Swapping in the array is enough — the bucket renders in array order.
  function nudge(task: Task, dir: -1 | 1): void {
    if (tasks === null) return;
    const bucket = task.group_id;
    const inBucket = tasksInBucket(bucket);
    const idx = inBucket.findIndex((t) => t.id === task.id);
    const swapWith = inBucket[idx + dir];
    if (!swapWith) return;
    const next = tasks.filter((t) => t.id !== task.id);
    const at = next.indexOf(swapWith) + (dir === 1 ? 1 : 0);
    next.splice(at, 0, task);
    setTasks(next);
    void persistOrder(bucket, next);
  }

  // Pointer drag: reorder within the same bucket by hovering another card. Uses
  // elementFromPoint over live state (no manual DOM), so React owns the reflow.
  function startDrag(task: Task, e: React.PointerEvent): void {
    e.preventDefault();
    dragId.current = task.id;
    const bucket = task.group_id;
    const onMoveEv = (ev: PointerEvent) => {
      const el = document.elementFromPoint(ev.clientX, ev.clientY);
      const card = el?.closest<HTMLElement>("[data-task-id]");
      const overId = card?.dataset.taskId;
      if (!overId || overId === dragId.current) return;
      setTasks((list) => {
        if (list === null) return list;
        const dragging = list.find((t) => t.id === dragId.current);
        const over = list.find((t) => t.id === overId);
        if (!dragging || !over || over.group_id !== bucket || dragging.group_id !== bucket) {
          return list;
        }
        const next = list.filter((t) => t.id !== dragging.id);
        next.splice(next.indexOf(over), 0, dragging);
        return next;
      });
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMoveEv);
      window.removeEventListener("pointerup", onUp);
      dragId.current = null;
      // Persist the bucket's settled order.
      setTasks((list) => {
        if (list !== null) void persistOrder(bucket, list);
        return list;
      });
    };
    window.addEventListener("pointermove", onMoveEv);
    window.addEventListener("pointerup", onUp);
  }

  async function commitRename(groupId: string): Promise<void> {
    const name = renameText.trim();
    setRenamingGroup(null);
    const current = groups.find((g) => g.id === groupId);
    if (!name || name === current?.name) return;
    setGroups((gs) => gs.map((g) => (g.id === groupId ? { ...g, name } : g)));
    try {
      await api.renameTaskGroup(groupId, name);
    } catch (err) {
      setToast(errorMessage(err));
      await refresh();
    }
  }

  async function deleteGroup(groupId: string): Promise<void> {
    setArmedDelete(null);
    try {
      await api.deleteTaskGroup(groupId);
      setToast("Group deleted — its tasks moved to Ungrouped.");
      if (filter === groupId) setFilter(ALL);
      await refresh();
    } catch (err) {
      setToast(errorMessage(err));
    }
  }

  // Fold/unfold a category, persisting the choice on this device. Keyed by the bucket
  // (a group id, or the Ungrouped sentinel). Reordering force-expands, so this only
  // fires from the settled "All" view header.
  function toggleCollapse(bucket: string | null): void {
    const key = bucket ?? UNGROUPED_KEY;
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      writeCollapsed(next);
      return next;
    });
  }

  function renderBucket(bucket: string | null, name: string, showHeader: boolean) {
    const list = tasksInBucket(bucket);
    if (list.length === 0 && bucket === null) return null; // no empty Ungrouped header
    // Collapse applies only where headers show (the "All" view) and never mid-reorder,
    // so a folded category can't hide the cards you're dragging.
    const isCollapsed = showHeader && !reordering && collapsed.has(bucket ?? UNGROUPED_KEY);
    return (
      <div key={bucket ?? UNGROUPED} className="task-bucket">
        {showHeader && (
          <div className="task-grouphdr">
            {reordering && bucket !== null && renamingGroup === bucket ? (
              <>
                <input
                  className="task-grename"
                  // biome-ignore lint/a11y/noAutofocus: the header morphed into an input on the user's tap.
                  autoFocus
                  aria-label="Rename group"
                  value={renameText}
                  onChange={(e) => setRenameText(e.target.value)}
                  onBlur={() => void commitRename(bucket)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void commitRename(bucket);
                    if (e.key === "Escape") setRenamingGroup(null);
                  }}
                />
                <span className="task-gcount">{list.length}</span>
              </>
            ) : reordering ? (
              <>
                <h3 className="task-gname">{name}</h3>
                <span className="task-gcount">{list.length}</span>
              </>
            ) : (
              <button
                type="button"
                className="task-gtoggle"
                aria-expanded={!isCollapsed}
                aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${name}`}
                onClick={() => toggleCollapse(bucket)}
              >
                <span className={`task-gchev${isCollapsed ? "" : " open"}`}>
                  <ChevronRightIcon size={14} />
                </span>
                <h3 className="task-gname">{name}</h3>
                <span className="task-gcount">{list.length}</span>
              </button>
            )}
            {reordering && bucket !== null && renamingGroup !== bucket && (
              <span className="task-gacts">
                <button
                  type="button"
                  className="task-gact"
                  aria-label={`Rename ${name}`}
                  onClick={() => {
                    setRenameText(name);
                    setRenamingGroup(bucket);
                  }}
                >
                  <PencilIcon size={15} />
                </button>
                <button
                  type="button"
                  className={`task-gact${armedDelete === bucket ? " armed" : ""}`}
                  aria-label={armedDelete === bucket ? `Confirm delete ${name}` : `Delete ${name}`}
                  onClick={() =>
                    armedDelete === bucket ? void deleteGroup(bucket) : setArmedDelete(bucket)
                  }
                >
                  {armedDelete === bucket ? "delete?" : <TrashIcon size={15} />}
                </button>
              </span>
            )}
          </div>
        )}
        {!isCollapsed &&
          list.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              open={openId === task.id}
              running={running.has(task.id)}
              reordering={reordering}
              unviewed={isUnviewed(task, viewed)}
              runs={openId === task.id ? (runsByTask[task.id] ?? null) : null}
              onToggleOpen={() => toggleOpen(task.id)}
              onFlip={() => void flip(task)}
              onRun={() => void runNow(task)}
              onEdit={() => setDraft(draftFrom(task))}
              onMove={() => setMoveFor(task)}
              onOpenRun={(run) => openRun(task, run)}
              onDragStart={(e) => startDrag(task, e)}
              onNudge={(dir) => nudge(task, dir)}
            />
          ))}
      </div>
    );
  }

  if (draft !== null) {
    return (
      <Editor
        draft={draft}
        onChange={setDraft}
        onClose={() => setDraft(null)}
        onSave={() => void save()}
        saving={saving}
      />
    );
  }

  const ungroupedCount = tasksInBucket(null).length;
  const hasAny = (tasks?.length ?? 0) > 0;

  return (
    <section className="runs-screen">
      <header className="runs-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back to launcher">
          <ChevronLeftIcon size={22} />
        </button>
        <h2 className="runs-bar-title">Tasks</h2>
        <button
          type="button"
          className={`icon-btn${reordering ? " on" : ""}`}
          onClick={() => {
            setReordering((r) => !r);
            setRenamingGroup(null);
            setArmedDelete(null);
          }}
          aria-label={reordering ? "Done organizing" : "Organize groups and order"}
          aria-pressed={reordering}
        >
          <ReorderIcon size={20} />
        </button>
        <button
          type="button"
          className="icon-btn"
          onClick={() => setDraft(draftFrom(null))}
          aria-label="New task"
        >
          <PlusIcon size={22} />
        </button>
        <button type="button" className="icon-btn" onClick={refresh} aria-label="Refresh">
          <RefreshIcon size={20} />
        </button>
      </header>

      {hasAny && (
        <div className="task-chiprow" role="tablist" aria-label="Group filter">
          <button
            type="button"
            role="tab"
            aria-selected={filter === ALL}
            className={`task-chip${filter === ALL ? " on" : ""}`}
            onClick={() => setFilter(ALL)}
          >
            All <span className="task-chip-n">{tasks?.length ?? 0}</span>
          </button>
          {groups.map((g) => (
            <button
              type="button"
              key={g.id}
              role="tab"
              aria-selected={filter === g.id}
              className={`task-chip${filter === g.id ? " on" : ""}`}
              onClick={() => setFilter(g.id)}
            >
              {g.name} <span className="task-chip-n">{tasksInBucket(g.id).length}</span>
            </button>
          ))}
          {ungroupedCount > 0 && (
            <button
              type="button"
              role="tab"
              aria-selected={filter === UNGROUPED}
              className={`task-chip${filter === UNGROUPED ? " on" : ""}`}
              onClick={() => setFilter(UNGROUPED)}
            >
              Ungrouped <span className="task-chip-n">{ungroupedCount}</span>
            </button>
          )}
        </div>
      )}

      <div className="runs-body">
        {error !== null && (
          <p className="error" role="alert">
            {error}
          </p>
        )}

        {reordering && (
          <p className="task-reorder-note">
            Drag the grip to reorder within a group, or focus it and use ↑ ↓. Rename or delete a
            group from its header.
          </p>
        )}

        {tasks === null && error === null ? (
          <p className="muted">Loading tasks…</p>
        ) : !hasAny ? (
          <p className="muted runs-empty">
            No tasks yet — create one to have an agent run a prompt for you.
          </p>
        ) : filter === ALL ? (
          <>
            {groups.map((g) => renderBucket(g.id, g.name, true))}
            {renderBucket(null, "Ungrouped", true)}
          </>
        ) : filter === UNGROUPED ? (
          renderBucket(null, "Ungrouped", false)
        ) : (
          renderBucket(filter, groups.find((g) => g.id === filter)?.name ?? "", false)
        )}

        {openId !== null && !reordering && tasks?.some((t) => t.id === openId) && (
          <div className="task-delwrap">
            <button
              type="button"
              className="task-del"
              onClick={() => {
                const t = tasks?.find((x) => x.id === openId);
                if (t) void remove(t);
              }}
            >
              Delete this task
            </button>
          </div>
        )}
      </div>

      {moveFor !== null && (
        <MoveTaskSheet
          taskName={moveFor.name || "task"}
          currentGroupId={moveFor.group_id}
          groups={groups}
          onMove={(gid) => void moveTo(moveFor, gid)}
          onCreateAndMove={(name) => void moveToNewGroup(moveFor, name)}
          onClose={() => setMoveFor(null)}
        />
      )}

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
