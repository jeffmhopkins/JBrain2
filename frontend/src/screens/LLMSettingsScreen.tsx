import { useEffect, useMemo, useRef, useState } from "react";
import type {
  LlmProviderId,
  LlmSettings,
  LlmTask,
  LocalModelInfo,
  ReasoningEffort,
} from "../api/client";
import { api } from "../api/client";
import { useForeground } from "../visibility";
import { AiUsageCard } from "./aiUsage";

// Strategy C — tasks are tiered by role. The grouping lives in the frontend
// (the wire is a flat task list); any task the API returns outside these
// tiers lands in a synthesized "Other" group so nothing is silently dropped.
interface GroupDef {
  key: string;
  /** Accent class flips the group's left rail (docs/DESIGN.md accents). */
  accent: "high" | "light" | "vision";
  name: string;
  desc: string;
  taskIds: string[];
}

// Groups mirror the prompts' `strength:` so the screen tells the truth about
// which work is heavy: note.extract/integrate.note/agent.turn are `high`,
// entity.disambiguate/session.title are `low`. fact.adjudicate &
// correction_note.extract have no prompt yet — placed by their design intent
// (docs/ANALYSIS.md: adjudicate=cheap, correction=strong). A task the API
// returns outside these defs lands in a synthesized "Other" group, so new
// routable tasks are never silently dropped.
const GROUP_DEFS: GroupDef[] = [
  {
    key: "high",
    accent: "high",
    name: "High-stakes reasoning",
    desc: "The hard judgment calls — worth deeper thinking.",
    taskIds: ["agent.turn", "integrate.note", "note.extract", "correction_note.extract"],
  },
  {
    key: "light",
    accent: "light",
    name: "Lightweight",
    desc: "Cheap, frequent extraction & one-shots.",
    taskIds: ["entity.disambiguate", "fact.adjudicate", "session.title"],
  },
  {
    key: "vision",
    accent: "vision",
    name: "Vision",
    desc: "Anything that reads or describes images.",
    taskIds: ["vision.ocr", "vision.caption"],
  },
];

const REASONING_LABEL: Record<ReasoningEffort, string> = {
  none: "None",
  low: "Low",
  medium: "Med",
  high: "High",
};

// Per-task override rows are tight (name + provider + reasoning on one line), so
// the override control uses single-letter labels to stay inside the card.
const REASONING_ABBR: Record<ReasoningEffort, string> = {
  none: "N",
  low: "L",
  medium: "M",
  high: "H",
};

interface ResolvedGroup extends GroupDef {
  tasks: LlmTask[];
}

// Partition the flat task list into the tier defs, in def order, with leftover
// tasks appended to a single fallback group. A def with no live tasks drops out.
function groupTasks(tasks: LlmTask[]): ResolvedGroup[] {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const claimed = new Set<string>();
  const groups: ResolvedGroup[] = [];
  for (const def of GROUP_DEFS) {
    const members = def.taskIds.flatMap((id) => {
      const task = byId.get(id);
      if (!task) return [];
      claimed.add(id);
      return [task];
    });
    if (members.length > 0) groups.push({ ...def, tasks: members });
  }
  const leftover = tasks.filter((t) => !claimed.has(t.id));
  if (leftover.length > 0) {
    groups.push({
      key: "other",
      accent: "light",
      name: "Other",
      desc: "Tasks not yet sorted into a tier.",
      taskIds: leftover.map((t) => t.id),
      tasks: leftover,
    });
  }
  return groups;
}

// A group's provider/reasoning are shared when its tasks agree, else "mixed":
// the per-task overrides have diverged and the group controls show that.
type GroupProvider = LlmProviderId | "mixed";
type GroupReasoning = ReasoningEffort | "mixed";

function sharedProvider(tasks: LlmTask[]): GroupProvider {
  const first = tasks[0]?.provider;
  return first !== undefined && tasks.every((t) => t.provider === first) ? first : "mixed";
}

function sharedReasoning(
  tasks: LlmTask[],
  reasons: (provider: LlmProviderId) => boolean,
): GroupReasoning {
  const reasoning = tasks.filter((t) => reasons(t.provider));
  if (reasoning.length === 0) return "mixed";
  const first = reasoning[0]?.reasoning_effort;
  return first != null && reasoning.every((t) => t.reasoning_effort === first) ? first : "mixed";
}

export function LLMSettingsScreen() {
  const [settings, setSettings] = useState<LlmSettings | null>(null);

  useEffect(() => {
    let stale = false;
    api
      .getLlmSettings()
      .then((s) => {
        if (!stale) setSettings(s);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);

  // Which tiers have their per-task overrides expanded.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [localOpen, setLocalOpen] = useState(false);
  // Catalog ids with a per-model action (stage/load/unload/window) in flight — the
  // row's controls show a pending state and lock out a second concurrent action.
  const [busy, setBusy] = useState<Set<string>>(new Set());

  // Live runtime state: while the drawer is open and hosting is on, refresh the
  // loaded flags every few seconds. Merge ONLY local_models so a poll can't
  // clobber an in-flight provider/reasoning edit. A backgrounded app suspends
  // the poll (re-runs with an immediate tick on return).
  const hostingEnabled = settings?.local_hosting_enabled ?? false;
  const foreground = useForeground();
  useEffect(() => {
    if (!localOpen || !hostingEnabled || !foreground) return;
    let stop = false;
    const tick = () =>
      api
        .getLlmSettings()
        .then((fresh) => {
          if (stop) return;
          setSettings((prev) =>
            prev
              ? {
                  ...prev,
                  host_memory: fresh.host_memory,
                  local_models: prev.local_models.map((m) => ({
                    ...m,
                    loaded: fresh.local_models.find((f) => f.id === m.id)?.loaded ?? m.loaded,
                  })),
                }
              : prev,
          );
        })
        .catch(() => {});
    const id = setInterval(tick, 4000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [localOpen, hostingEnabled, foreground]);

  const mark = (id: string) => setBusy((s) => new Set(s).add(id));
  const unmark = (id: string) =>
    setBusy((s) => {
      const next = new Set(s);
      next.delete(id);
      return next;
    });
  // Sequence token so an earlier PUT's response can't clobber a later one (and a
  // response after unmount is ignored). Shared across task + per-model writes —
  // the server snapshot is the source of truth, so last response in wins.
  const putSeq = useRef(0);

  // load/unload return just the resident set; reconcile the loaded flags in place
  // (a poll-style merge) so an in-flight task edit isn't clobbered.
  function reconcileLoaded(res: { loaded: string[] }) {
    const loaded = new Set(res.loaded);
    setSettings((prev) =>
      prev
        ? {
            ...prev,
            local_models: prev.local_models.map((m) => ({ ...m, loaded: loaded.has(m.id) })),
          }
        : prev,
    );
  }

  function unloadModel(id: string) {
    mark(id);
    api
      .unloadLocalModel(id)
      .then(reconcileLoaded)
      .catch(() => {})
      .finally(() => unmark(id));
  }

  function loadModel(id: string) {
    mark(id);
    api
      .loadLocalModel(id)
      .then(reconcileLoaded)
      .catch(() => {})
      .finally(() => unmark(id));
  }

  // stage + context-window return the full snapshot; reconcile it whole (guarded).
  function stageModel(id: string, on: boolean) {
    mark(id);
    const seq = ++putSeq.current;
    api
      .stageLocalModel(id, on)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark(id));
  }

  function setContextWindow(id: string, window: number | null) {
    mark(id);
    const seq = ++putSeq.current;
    api
      .setLocalContextWindow(id, window)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark(id));
  }

  const groups = useMemo(() => (settings ? groupTasks(settings.tasks) : []), [settings]);

  if (settings === null) {
    return (
      <main className="screen-body settings">
        <p className="settings-meta">Loading…</p>
      </main>
    );
  }

  const providers = settings.providers;
  const efforts = settings.reasoning_efforts;
  const defaultEffort = settings.reasoning_default;

  // Vision tasks may only run on vision-capable providers (the cloud models, or
  // a vision local model) — a text-only local model can't read images.
  const visionProviders = providers.filter((p) => p.supports_vision);
  const providersFor = (isVision: boolean) => (isVision ? visionProviders : providers);
  const isVisionTask = (taskId: string) => taskId.startsWith("vision.");
  const byId = new Map(providers.map((p) => [p.id, p]));
  // Reasoning is a per-provider capability (today only grok), read from the wire
  // flag rather than a hardcoded id so a future reasoning-capable provider works.
  const reasonOn = (id: string) => byId.get(id)?.supports_reasoning ?? false;
  // Snapshot the tasks past the null guard so the wire-builder closure below
  // reads them without TS re-widening the `settings` state back to nullable.
  const currentTasks = settings.tasks;

  // Apply a patch optimistically, then PUT only the touched tasks and reconcile
  // from the response. A task on a reasoning-capable provider (Grok or a local
  // gpt-oss/GLM) carries its reasoning level; off one it drops.
  function applyTasks(updates: Map<string, LlmTaskPatchLocal>) {
    setSettings((prev) => {
      if (prev === null) return prev;
      return {
        ...prev,
        tasks: prev.tasks.map((t) => {
          const u = updates.get(t.id);
          if (!u) return t;
          const provider = u.provider ?? t.provider;
          const effort = reasonOn(provider)
            ? (u.reasoning_effort ?? t.reasoning_effort ?? defaultEffort)
            : null;
          return { ...t, provider, reasoning_effort: effort };
        }),
      };
    });

    const wire: Record<string, { provider: LlmProviderId; reasoning_effort?: ReasoningEffort }> =
      {};
    for (const [id, u] of updates) {
      const task = currentTasks.find((t) => t.id === id);
      const provider = u.provider ?? task?.provider ?? "grok";
      wire[id] = reasonOn(provider)
        ? {
            provider,
            reasoning_effort: u.reasoning_effort ?? task?.reasoning_effort ?? defaultEffort,
          }
        : { provider };
    }
    const seq = ++putSeq.current;
    void api
      .updateLlmSettings({ tasks: wire })
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {});
  }

  function setGroupProvider(group: ResolvedGroup, provider: LlmProviderId) {
    const updates = new Map<string, LlmTaskPatchLocal>();
    for (const t of group.tasks) updates.set(t.id, { provider });
    applyTasks(updates);
  }

  function setGroupReasoning(group: ResolvedGroup, effort: ReasoningEffort) {
    const updates = new Map<string, LlmTaskPatchLocal>();
    // Only reasoning-capable tasks carry a level; others are untouched.
    for (const t of group.tasks) {
      if (reasonOn(t.provider)) updates.set(t.id, { reasoning_effort: effort });
    }
    applyTasks(updates);
  }

  function setTaskProvider(taskId: string, provider: LlmProviderId) {
    applyTasks(new Map([[taskId, { provider }]]));
  }

  function setTaskReasoning(taskId: string, effort: ReasoningEffort) {
    applyTasks(new Map([[taskId, { reasoning_effort: effort }]]));
  }

  function toggleExpanded(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <main className="screen-body settings">
      <p className="settings-meta">
        Tasks are grouped by role. Set the provider and reasoning once per tier; expand a tier to
        fine-tune individual tasks that should diverge.
      </p>

      <LocalModelsDrawer
        open={localOpen}
        onToggle={() => setLocalOpen((v) => !v)}
        hostingEnabled={settings.local_hosting_enabled}
        models={settings.local_models}
        hostMemory={settings.host_memory}
        busy={busy}
        onUnload={unloadModel}
        onLoad={loadModel}
        onStage={stageModel}
        onSetWindow={setContextWindow}
      />

      {groups.map((group) => {
        const provider = sharedProvider(group.tasks);
        const reasoning = sharedReasoning(group.tasks, reasonOn);
        const reasoningOn = provider !== "mixed" && reasonOn(provider);
        const isOpen = expanded.has(group.key);
        const groupVision = group.accent === "vision";
        // The current provider may not be in the (filtered) option list — e.g. a
        // task pinned to a local model after hosting was turned off. Surface it as
        // a disabled option so the select shows the truth and can't be silently
        // overwritten (mirrors the `mixed` handling).
        const optMissing =
          provider !== "mixed" && !providersFor(groupVision).some((p) => p.id === provider);
        // Claude gets its own wording; any other non-reasoning provider (local
        // models) shares the generic note; reasoning-capable or mixed → no note.
        const naNote =
          provider === "claude"
            ? "Claude manages thinking on its own."
            : reasoningOn || provider === "mixed"
              ? null
              : "This model takes no reasoning level.";

        return (
          <section key={group.key} className={`llm-group llm-${group.accent}`}>
            <div className="llm-group-head">
              <div className="llm-group-title">
                <span className="llm-group-name">{group.name}</span>
                <span className="llm-group-count">{group.tasks.length} tasks</span>
              </div>
              <p className="llm-group-desc">{group.desc}</p>

              <span className="llm-field-tag">Provider</span>
              <select
                className="llm-select"
                aria-label={`${group.name} provider`}
                value={provider}
                onChange={(e) => setGroupProvider(group, e.target.value as LlmProviderId)}
              >
                {provider === "mixed" && (
                  <option value="mixed" disabled>
                    Mixed
                  </option>
                )}
                {optMissing && (
                  <option value={provider} disabled>
                    {provider} (unavailable)
                  </option>
                )}
                {providersFor(groupVision).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
              </select>

              <span className="llm-field-tag">Reasoning</span>
              {reasoningOn ? (
                <fieldset className="seg-row llm-seg-row" aria-label={`${group.name} reasoning`}>
                  {efforts.map((effort) => (
                    <button
                      key={effort}
                      type="button"
                      className={`seg${reasoning === effort ? " seg-on" : ""}`}
                      aria-pressed={reasoning === effort}
                      onClick={() => setGroupReasoning(group, effort)}
                    >
                      {REASONING_LABEL[effort]}
                    </button>
                  ))}
                </fieldset>
              ) : (
                <p className="llm-na-note">{naNote}</p>
              )}
            </div>

            <div className="llm-expand">
              <button
                type="button"
                className="llm-exp-toggle"
                aria-expanded={isOpen}
                onClick={() => toggleExpanded(group.key)}
              >
                <span>Per-task overrides</span>
                <span
                  className={`llm-exp-caret${isOpen ? " llm-exp-open" : ""}`}
                  aria-hidden="true"
                >
                  ›
                </span>
              </button>
              {isOpen && (
                <div className="llm-members">
                  {group.tasks.map((task) => {
                    const taskReasons = reasonOn(task.provider);
                    const taskOpts = providersFor(isVisionTask(task.id));
                    const taskMissing = !taskOpts.some((p) => p.id === task.provider);
                    return (
                      <div key={task.id} className="llm-member">
                        <div className="llm-member-name">
                          {task.label}
                          <span className="llm-member-id">{task.id}</span>
                        </div>
                        <div className="llm-member-controls">
                          <select
                            className="llm-select llm-member-select"
                            aria-label={`${task.label} provider`}
                            value={task.provider}
                            onChange={(e) =>
                              setTaskProvider(task.id, e.target.value as LlmProviderId)
                            }
                          >
                            {taskMissing && (
                              <option value={task.provider} disabled>
                                {task.provider} (unavailable)
                              </option>
                            )}
                            {taskOpts.map((p) => (
                              <option key={p.id} value={p.id}>
                                {p.label}
                              </option>
                            ))}
                          </select>
                          {taskReasons ? (
                            <fieldset
                              className="seg-row llm-seg-row llm-member-seg"
                              aria-label={`${task.label} reasoning`}
                            >
                              {efforts.map((effort) => (
                                <button
                                  key={effort}
                                  type="button"
                                  className={`seg${task.reasoning_effort === effort ? " seg-on" : ""}`}
                                  aria-pressed={task.reasoning_effort === effort}
                                  aria-label={REASONING_LABEL[effort]}
                                  title={REASONING_LABEL[effort]}
                                  onClick={() => setTaskReasoning(task.id, effort)}
                                >
                                  {REASONING_ABBR[effort]}
                                </button>
                              ))}
                            </fieldset>
                          ) : (
                            <span className="llm-member-na">n/a</span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </section>
        );
      })}

      <AiUsageCard />
    </main>
  );
}

// A local patch shape: either field may be absent (a provider-only or
// reasoning-only change). applyTasks reconciles the omitted field from state.
interface LlmTaskPatchLocal {
  provider?: LlmProviderId;
  reasoning_effort?: ReasoningEffort;
}

// Capability chips for a local model — same muted register as the rest of the
// chrome (docs/DESIGN.md), keyed by what the model can do.
function capabilityChips(m: LocalModelInfo) {
  const chips: { key: string; label: string; cls: string }[] = [];
  if (m.supports_vision) chips.push({ key: "vision", label: "vision", cls: "vision" });
  if (m.tiers.includes("high")) chips.push({ key: "reason", label: "reasoning", cls: "reason" });
  if (m.supports_tools) chips.push({ key: "tools", label: "tools", cls: "tools" });
  return chips;
}

// Positional bar palette: a segment's color comes from its SLOT on the memory bar
// (1st green, 2nd yellow, 3rd orange, …), not the model identity, so colors stay
// stable by position as models load/unload. [h, s, l]; weights sit at the base
// lightness and the context (KV) block a lighter tint, each block sweeping the hue
// ±HUE_SPREAD for a little depth.
const BAR_PALETTE: [number, number, number][] = [
  [138, 34, 58],
  [46, 50, 63],
  [28, 48, 62],
  [14, 50, 60],
];
const HUE_SPREAD = 14;
function slotGradient(slot: number, lighten = 0): string {
  const [h, s, l] = BAR_PALETTE[slot % BAR_PALETTE.length] ?? [0, 0, 50];
  const lt = Math.min(l + lighten, 88);
  return `linear-gradient(90deg, hsl(${h - HUE_SPREAD} ${s}% ${lt}%), hsl(${h + HUE_SPREAD} ${s}% ${lt}%))`;
}

// The size picker's choices, capped per model at its catalog window.
const WINDOW_CHOICES = [16384, 32768, 65536, 131072];
const fmtTokens = (n: number) => (n % 1024 === 0 ? `${n / 1024}k` : `${Math.round(n / 1000)}k`);
const barName = (m: LocalModelInfo) => m.label.split(" ")[0];
const residentGbOf = (m: LocalModelInfo) => (m.disk_gb ?? m.size_gb) + m.kv_gb;

// Roster of self-hosted models with a stage→load→unload lifecycle and a per-model
// context window. Provisioning (the weight download) is still a server-side step —
// `jbrain enable-local-models` — so only provisioned models appear; what shows here
// can be staged, loaded, unloaded, and re-sized live.
function LocalModelsDrawer({
  open,
  onToggle,
  hostingEnabled,
  models,
  hostMemory,
  busy,
  onUnload,
  onLoad,
  onStage,
  onSetWindow,
}: {
  open: boolean;
  onToggle: () => void;
  hostingEnabled: boolean;
  models: LocalModelInfo[];
  hostMemory: { total_gb: number; used_gb: number } | null;
  busy: Set<string>;
  onUnload: (id: string) => void;
  onLoad: (id: string) => void;
  onStage: (id: string, on: boolean) => void;
  onSetWindow: (id: string, window: number | null) => void;
}) {
  const enabledCount = models.filter((m) => m.enabled).length;
  const shown = models.filter((m) => m.enabled);
  const loaded = shown.filter((m) => m.loaded);
  const stagedOnly = shown.filter((m) => m.staged && !m.loaded);
  // Resident footprint = weights + KV for everything actually loaded.
  const residentGb = loaded.reduce((sum, m) => sum + residentGbOf(m), 0);
  const stagedGb = stagedOnly.reduce((sum, m) => sum + residentGbOf(m), 0);
  const stagedCount = stagedOnly.length;
  const summary = !hostingEnabled
    ? "off"
    : `${enabledCount} of ${models.length} enabled · ${loaded.length} loaded${
        stagedCount ? ` · ${stagedCount} staged` : ""
      } · ${Math.round(residentGb)} GB`;
  const ariaLabel = `Local models — ${hostingEnabled ? `hosting on, ${summary}` : "hosting off"}`;

  // Loaded segments first (resident), then staged (projected) — colored by slot.
  const onBar = [...loaded, ...stagedOnly];
  const total = hostMemory?.total_gb ?? 0;
  const projectedGb = residentGb + stagedGb;
  const over = total > 0 && projectedGb > total;

  return (
    <section className="llm-local">
      <button
        type="button"
        className="llm-local-toggle"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={onToggle}
      >
        <span className={`llm-local-dot${hostingEnabled ? " on" : ""}`} aria-hidden="true" />
        <span className="llm-local-title">Local models</span>
        <span className="llm-local-summary">{summary}</span>
        <span className={`llm-exp-caret${open ? " llm-exp-open" : ""}`} aria-hidden="true">
          ›
        </span>
      </button>

      {open && (
        <div className="llm-local-body">
          {hostMemory && total > 0 && (
            <div className="llm-mem" aria-label="unified memory in use">
              <div className="llm-mem-bar">
                {onBar.map((m, i) => {
                  const weights = m.disk_gb ?? m.size_gb;
                  const res = weights + m.kv_gb;
                  const isStaged = m.staged && !m.loaded;
                  return (
                    <div
                      key={m.id}
                      className={`llm-mem-seg${isStaged ? " staged" : ""}`}
                      style={{ width: `${(res / total) * 100}%` }}
                      title={`${m.label} — ${weights} GB weights + ${m.kv_gb} GB KV${
                        isStaged ? " (staged)" : ""
                      }`}
                    >
                      <div
                        className="llm-mem-w"
                        style={{ width: `${(weights / res) * 100}%`, background: slotGradient(i) }}
                      />
                      <div
                        className="llm-mem-c"
                        style={{
                          width: `${(m.kv_gb / res) * 100}%`,
                          background: slotGradient(i, 18),
                        }}
                      />
                      <span className="llm-mem-label">
                        {barName(m)} <span className="gb">{Math.round(res)}G</span>
                      </span>
                    </div>
                  );
                })}
              </div>
              <div className="llm-mem-cap">
                <span>{Math.round(residentGb)} GB used</span>
                {stagedGb > 0.05 && (
                  <span className={`staged-note${over ? " over" : ""}`}>
                    +{Math.round(stagedGb)} GB staged → {Math.round(projectedGb)} GB
                    {over ? " ⚠ over" : ""}
                  </span>
                )}
                <span className="total">{Math.round(total)} GB total</span>
              </div>
            </div>
          )}
          {!hostingEnabled && (
            <p className="llm-local-hint">
              Self-hosting is off. Provision on the server with{" "}
              <code>jbrain enable-local-models</code>; models you enable there become selectable in
              the tiers above.
            </p>
          )}
          {hostingEnabled && shown.length === 0 && (
            <p className="llm-local-hint">
              No models enabled yet — provision more with <code>jbrain enable-local-models</code>.
            </p>
          )}
          {shown.map((m) => {
            const footprint = m.disk_gb ?? m.size_gb;
            const sizeText = `${m.disk_gb == null ? "~" : ""}${footprint} GB`;
            const state = m.loaded ? "loaded" : m.staged ? "staged" : "idle";
            const editable = !m.loaded; // idle or staged — no live process to disrupt
            const isBusy = busy.has(m.id);
            const effWindow = m.context_window_override ?? m.context_window;
            const windowOpts = Array.from(
              new Set([...WINDOW_CHOICES.filter((w) => w <= m.context_window), m.context_window]),
            ).sort((a, b) => a - b);
            return (
              <div key={m.id} className={`llm-local-row on ${state}`}>
                <div className="llm-local-head">
                  <div className="llm-local-name">
                    {m.label}
                    <span className="llm-local-meta">
                      {m.quant} · {sizeText}
                      {m.loaded ? ` · ${Math.round(residentGbOf(m))} GB resident` : ""}
                    </span>
                  </div>
                  <div className="llm-local-topright">
                    <div className="llm-local-act">
                      {state === "idle" && (
                        <button
                          type="button"
                          className="llm-local-btn stage"
                          disabled={isBusy}
                          onClick={() => onStage(m.id, true)}
                        >
                          {isBusy ? "…" : "Stage"}
                        </button>
                      )}
                      {state === "staged" && (
                        <>
                          <button
                            type="button"
                            className="llm-local-btn load"
                            disabled={isBusy}
                            onClick={() => onLoad(m.id)}
                          >
                            {isBusy ? "…" : "Load"}
                          </button>
                          <button
                            type="button"
                            className="llm-local-btn"
                            disabled={isBusy}
                            onClick={() => onStage(m.id, false)}
                          >
                            Unstage
                          </button>
                        </>
                      )}
                      {state === "loaded" && (
                        <button
                          type="button"
                          className="llm-local-btn"
                          disabled={isBusy}
                          onClick={() => onUnload(m.id)}
                        >
                          {isBusy ? "…" : "Unload"}
                        </button>
                      )}
                    </div>
                    <span
                      className={`llm-local-state${m.loaded ? " on" : m.staged ? " staged" : ""}`}
                    >
                      {state}
                    </span>
                  </div>
                </div>
                <div className="llm-local-chips">
                  {capabilityChips(m).map((c) => (
                    <span key={c.key} className={`llm-chip llm-chip-${c.cls}`}>
                      {c.label}
                    </span>
                  ))}
                </div>
                <div className="llm-local-ctx">
                  <label className="llm-local-ctx-label" htmlFor={`ctx-${m.id}`}>
                    context window
                  </label>
                  <select
                    id={`ctx-${m.id}`}
                    className="llm-local-ctx-select"
                    value={String(effWindow)}
                    disabled={!editable || isBusy}
                    onChange={(e) => {
                      const v = Number(e.target.value);
                      // The catalog default is "no override" — store null for it so a
                      // redundant override row is never persisted.
                      onSetWindow(m.id, v === m.context_window ? null : v);
                    }}
                  >
                    {windowOpts.map((w) => (
                      <option key={w} value={w}>
                        {fmtTokens(w)}
                      </option>
                    ))}
                  </select>
                  {m.loaded ? (
                    <span className="llm-local-ctx-hint">🔒 unload to change</span>
                  ) : (
                    <span className="llm-local-ctx-meta">KV ~{m.kv_gb} GB</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
