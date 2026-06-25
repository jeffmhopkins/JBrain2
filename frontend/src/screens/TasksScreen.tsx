// The Tasks surface — saved prompts that spawn an agent session on a schedule or on
// demand (binding mock docs/mocks/tasks-launcher-a-list-editor.html, Direction A).
// A self-contained full-screen overlay (its own back bar), like Automations. Cards
// read "agent · schedule" with an enable toggle + next/last-run meta; expanding one
// loads its recent runs (each links to the session it produced). "＋ New task" rises
// a full-screen editor: prompt, agent (Curator reveals a scope dial), schedule, and
// delivery. Reflects live state — the cards come from /api/tasks.

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  type ScheduleFreq,
  type ScheduleKind,
  type Task,
  type TaskAgent,
  type TaskInput,
  type TaskRun,
  api,
} from "../api/client";
import { TASKS_SEEN_KEY } from "../components/Launcher";
import {
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  PlayIcon,
  PlusIcon,
  RefreshIcon,
  XIcon,
} from "../components/icons";

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

interface CardProps {
  task: Task;
  open: boolean;
  running: boolean;
  runs: TaskRun[] | null;
  onToggleOpen: () => void;
  onFlip: () => void;
  onRun: () => void;
  onEdit: () => void;
  onOpenSession: (sessionId: string) => void;
}

function TaskCard({
  task,
  open,
  running,
  runs,
  onToggleOpen,
  onFlip,
  onRun,
  onEdit,
  onOpenSession,
}: CardProps) {
  const dot = running ? "running" : !task.enabled ? "idle" : "ok";
  return (
    <div className={`task-card${task.enabled ? "" : " off"}${open ? " open" : ""}`}>
      <div className="task-head">
        <button type="button" className="task-headbtn" onClick={onToggleOpen}>
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
          <ChevronRightIcon size={15} />
        </button>
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
      </div>
      {open && (
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
                  onClick={() => onOpenSession(r.session_id as string)}
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
              <PlusIcon size={14} />
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
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [running, setRunning] = useState<Set<string>>(new Set());
  const [runsByTask, setRunsByTask] = useState<Record<string, TaskRun[]>>({});
  const [toast, setToast] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      setTasks(await api.tasks());
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Opening Tasks marks every run so far as "seen", so the launcher's Tasks badge
  // (runs since last opened) clears the next time the menu is shown.
  useEffect(() => {
    try {
      localStorage.setItem(TASKS_SEEN_KEY, new Date().toISOString());
    } catch {
      // best-effort; the launcher re-seeds a missing marker
    }
  }, []);

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

  const scheduled = tasks?.filter((t) => t.schedule_kind !== "on_demand") ?? [];
  const onDemand = tasks?.filter((t) => t.schedule_kind === "on_demand") ?? [];

  function renderGroup(label: string, group: Task[]) {
    if (group.length === 0) return null;
    return (
      <div>
        <h3 className="runs-sect">{label}</h3>
        {group.map((task) => (
          <TaskCard
            key={task.id}
            task={task}
            open={openId === task.id}
            running={running.has(task.id)}
            runs={openId === task.id ? (runsByTask[task.id] ?? null) : null}
            onToggleOpen={() => toggleOpen(task.id)}
            onFlip={() => void flip(task)}
            onRun={() => void runNow(task)}
            onEdit={() => setDraft(draftFrom(task))}
            onOpenSession={(sessionId) => onOpenSession(sessionId, task.agent)}
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

  return (
    <section className="runs-screen">
      <header className="runs-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back to launcher">
          <ChevronLeftIcon size={22} />
        </button>
        <h2 className="runs-bar-title">Tasks</h2>
        <button
          type="button"
          className="icon-btn runs-refresh"
          onClick={() => setDraft(draftFrom(null))}
          aria-label="New task"
        >
          <PlusIcon size={22} />
        </button>
        <button type="button" className="icon-btn" onClick={refresh} aria-label="Refresh">
          <RefreshIcon size={20} />
        </button>
      </header>

      <div className="runs-body">
        {error !== null && (
          <p className="error" role="alert">
            {error}
          </p>
        )}

        {tasks === null && error === null ? (
          <p className="muted">Loading tasks…</p>
        ) : tasks !== null && tasks.length === 0 ? (
          <p className="muted runs-empty">
            No tasks yet — create one to have an agent run a prompt for you.
          </p>
        ) : (
          <>
            {renderGroup("Scheduled", scheduled)}
            {renderGroup("On demand", onDemand)}
          </>
        )}

        {openId !== null && tasks?.some((t) => t.id === openId) && (
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
