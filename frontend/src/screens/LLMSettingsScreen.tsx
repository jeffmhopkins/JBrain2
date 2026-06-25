import type { CSSProperties, ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  ImageModelInfo,
  ImageSettings,
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
// entity.disambiguate/session.title/triage.classify are `low`. fact.adjudicate &
// correction_note.extract have no prompt yet — placed by their design intent
// (docs/ANALYSIS.md: adjudicate=cheap, correction=strong). video.summarize is the
// analyze_video reduce step — owner-placed in high-stakes for a richer summary,
// though its prompt is `low`. wiki.rewrite/wiki.ground (the Phase-6 builder's draft
// + grounding-verify pass) are reasoning-heavy, so they sit in high-stakes too. A
// task the API returns outside these defs lands in a synthesized "Other" group, so
// new routable tasks are never silently dropped.
const GROUP_DEFS: GroupDef[] = [
  {
    key: "high",
    accent: "high",
    name: "High-stakes reasoning",
    desc: "The hard judgment calls — worth deeper thinking.",
    taskIds: [
      "agent.turn",
      "integrate.note",
      "note.extract",
      "correction_note.extract",
      "video.summarize",
      "wiki.rewrite",
      "wiki.ground",
    ],
  },
  {
    key: "light",
    accent: "light",
    name: "Lightweight",
    desc: "Cheap, frequent extraction & one-shots.",
    taskIds: ["entity.disambiguate", "fact.adjudicate", "session.title", "triage.classify"],
  },
  {
    key: "vision",
    accent: "vision",
    name: "Vision",
    desc: "Anything that reads or describes images.",
    taskIds: ["vision.ocr", "vision.caption", "agent.vision"],
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
  // The on-box image service, surfaced in the same drawer (shared meter). Fetched
  // alongside the LLM settings; null until it loads / when the feature is absent.
  const [image, setImage] = useState<ImageSettings | null>(null);

  useEffect(() => {
    let stale = false;
    api
      .getLlmSettings()
      .then((s) => {
        if (!stale) setSettings(s);
      })
      .catch(() => {});
    api
      .getImageSettings()
      .then((s) => {
        if (!stale) setImage(s);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);

  // Which tiers have their per-task overrides expanded.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // The On-box card's surface switch: the residency ladder (lanes) or the library
  // (catalog). The ladder is the common case, so it opens by default.
  const [surface, setSurface] = useState<OnBoxSurface>("lanes");
  // Catalog ids with a per-model action (stage/load/unload/window) in flight — the
  // row's controls show a pending state and lock out a second concurrent action.
  const [busy, setBusy] = useState<Set<string>>(new Set());

  // Live runtime state: while the drawer is open and hosting is on, refresh the
  // loaded flags every few seconds. Merge ONLY local_models so a poll can't
  // clobber an in-flight provider/reasoning edit. A backgrounded app suspends
  // the poll (re-runs with an immediate tick on return).
  const hostingEnabled = settings?.local_hosting_enabled ?? false;
  const imageEnabled = image?.enabled ?? false;
  const foreground = useForeground();
  useEffect(() => {
    // The card is always showing (the gauge + a surface), so refresh whenever the
    // app is foregrounded and any backend is on.
    if (!foreground || (!hostingEnabled && !imageEnabled)) return;
    let stop = false;
    const tick = () => {
      if (hostingEnabled) {
        api
          .getLlmSettings()
          .then((fresh) => {
            if (stop) return;
            setSettings((prev) =>
              prev
                ? {
                    ...prev,
                    host_memory: fresh.host_memory,
                    // Carry the live runtime + install-progress fields (loaded,
                    // and an in-flight install's queued/enabled/download_gb) so a
                    // download bar climbs and a finished install flips to enabled
                    // without a manual refresh — but not the operator's in-flight
                    // window/staged edits, which the snapshot may not yet reflect.
                    local_models: prev.local_models.map((m) => {
                      const f = fresh.local_models.find((x) => x.id === m.id);
                      return f
                        ? {
                            ...m,
                            loaded: f.loaded,
                            enabled: f.enabled,
                            queued: f.queued,
                            // Carry the uninstall flag too, so a queued uninstall and
                            // its eventual disappearance (model leaves LOCAL_MODELS →
                            // enabled:false) reconcile live, like install's queued.
                            remove_queued: f.remove_queued,
                            disk_gb: f.disk_gb,
                            download_gb: f.download_gb,
                          }
                        : m;
                    }),
                  }
                : prev,
            );
          })
          .catch(() => {});
      }
      // The image service has no per-model load flag; its VRAM gauge is the live
      // signal, so re-pull the whole snapshot (cheap, owner-only).
      if (imageEnabled) {
        api
          .getImageSettings()
          .then((fresh) => {
            if (!stop) setImage(fresh);
          })
          .catch(() => {});
      }
    };
    const id = setInterval(tick, 4000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [hostingEnabled, imageEnabled, foreground]);

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

  // The ladder's resident→staged step (one rung down). The backend has no single
  // "unload but keep staged" call, so sequence the two existing ones: unload the
  // process, then stage it so the tile lands in the STAGED lane (never resident→
  // unloaded directly). The full snapshot from stage reconciles last; the putSeq
  // guard keeps a stale 4s poll from clobbering the result. This is the only
  // unload path on the ladder — a resident model never drops straight to idle.
  function unloadToStaged(id: string) {
    mark(id);
    const seq = ++putSeq.current;
    api
      .unloadLocalModel(id)
      .then(() => api.stageLocalModel(id, true))
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

  // Queue / unqueue an un-provisioned model for install; the snapshot reflects the
  // queued flag at once (the download itself happens during the next update).
  function queueInstall(id: string, on: boolean) {
    mark(id);
    const seq = ++putSeq.current;
    api
      .queueLocalInstall(id, on)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark(id));
  }

  // Queue / unqueue a provisioned model for uninstall — destructive (the next
  // update drops it from LOCAL_MODELS and prunes its weights), so queueing confirms.
  function queueUninstall(id: string, on: boolean) {
    if (on && !window.confirm("Uninstall this model and delete its weights on the next update?")) {
      return;
    }
    mark(id);
    const seq = ++putSeq.current;
    api
      .queueLocalUninstall(id, on)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark(id));
  }

  // "Update & install now": kick the supervisor update one-shot, which provisions
  // the queued models at its tail. We follow it two ways: the coarse phase from the
  // update log tail (here), and each model's live download bar (the snapshot poll).
  const [updateState, setUpdateState] = useState<"idle" | "running" | "failed">("idle");
  const [updateTail, setUpdateTail] = useState("");
  function startInstallUpdate() {
    setUpdateState("running");
    setUpdateTail("");
    api.opsUpdateStart().catch(() => setUpdateState("failed"));
  }
  useEffect(() => {
    if (updateState !== "running" || !foreground) return;
    let stop = false;
    const tick = () => {
      api
        .opsUpdateStatus()
        .then((s) => {
          if (stop) return;
          const lines = s.log_tail.trimEnd().split("\n");
          setUpdateTail(lines[lines.length - 1] ?? "");
          // The download runs inside the one-shot, so an exit means provisioning is
          // done (the snapshot poll has the models enabled by now).
          if (s.state === "exited") setUpdateState(s.exit_code === 0 ? "idle" : "failed");
        })
        .catch(() => {});
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [updateState, foreground]);

  // Image-service controls (owner-only): free unloads the resident model; start/stop
  // toggle the service via the supervisor. Each reconciles from a fresh snapshot.
  function freeImage() {
    api
      .freeImageMemory()
      .then(setImage)
      .catch(() => {});
  }
  function startImageService() {
    api
      .startImageService()
      .then(() => api.getImageSettings())
      .then(setImage)
      .catch(() => {});
  }
  function stopImageService() {
    api
      .stopImageService()
      .then(() => api.getImageSettings())
      .then(setImage)
      .catch(() => {});
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
  const isVisionTask = (taskId: string) =>
    taskId.startsWith("vision.") || taskId === "agent.vision";
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

      <OnBoxModelsCard
        surface={surface}
        onSurface={setSurface}
        hostingEnabled={settings.local_hosting_enabled}
        models={settings.local_models}
        hostMemory={settings.host_memory}
        image={image}
        busy={busy}
        onUnloadToStaged={unloadToStaged}
        onLoad={loadModel}
        onStage={stageModel}
        onSetWindow={setContextWindow}
        onInstall={queueInstall}
        onUninstall={queueUninstall}
        onUpdate={startInstallUpdate}
        updateState={updateState}
        updateTail={updateTail}
        onFreeImage={freeImage}
        onStartImageService={startImageService}
        onStopImageService={stopImageService}
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

// ComfyUI reports VRAM, not a per-model load flag; treat a draw above this as "a
// model is resident" so the gauge shows it. An estimate — tune on the box.
const IMG_ACTIVE_GB = 4;

// The size picker's choices, capped per model at its catalog window.
const WINDOW_CHOICES = [16384, 32768, 65536, 131072];
const fmtTokens = (n: number) => (n % 1024 === 0 ? `${n / 1024}k` : `${Math.round(n / 1000)}k`);
const firstWord = (label: string) => label.split(" ")[0] ?? label;
// Resident/staged footprint for an LLM = weights (on-disk, or the estimate) + KV.
const residentGbOf = (m: LocalModelInfo) => (m.disk_gb ?? m.size_gb) + m.kv_gb;
// An image model's resident draw is its VRAM estimate; the service holds one at a time.
const imgFootGb = (m: ImageModelInfo) => m.vram_gb;

// The On-box card's surface switch: the residency ladder (lanes) or the library
// (catalog of installs/uninstalls). The two reuse the shared `.seg-row` register.
type OnBoxSurface = "lanes" | "catalog";

// Small inline glyphs for the surface switch (mirrors the mock's IC set): stacked
// rungs for the residency ladder, an open book for the library/catalog.
const SurfaceIcon = {
  lanes: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      <rect x="3" y="4" width="18" height="4.5" rx="1.5" />
      <rect x="3" y="10" width="18" height="4.5" rx="1.5" />
      <rect x="3" y="16" width="18" height="4.5" rx="1.5" />
    </svg>
  ),
  catalog: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10v16H5.5A1.5 1.5 0 0 1 4 18.5z" />
      <path d="M14 4h4.5A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5H14z" />
    </svg>
  ),
} as const;

// A model's place on the residency ladder. RESIDENT = loaded; STAGED = staged but
// not loaded; UNLOADED = neither. Image models never reach STAGED (no staging
// concept on the service), so they only ride the resident / unloaded rungs.
type Rung = "resident" | "staged" | "unloaded";

const reduceMotion = () =>
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// One card: a unified-memory capacity gauge (always visible), a residency/library
// surface switch, then the chosen surface. The single ~128 GB pool is shared by the
// LLM weights+KV (slot-colored), staged LLMs (ghosted/projected), and the image
// VRAM segment (violet). Installed LLMs ride a strict one-rung ladder; image models
// appear read-only on the lanes (their lifecycle is the service's, not per-model).
function OnBoxModelsCard({
  surface,
  onSurface,
  hostingEnabled,
  models,
  hostMemory,
  image,
  busy,
  onUnloadToStaged,
  onLoad,
  onStage,
  onSetWindow,
  onInstall,
  onUninstall,
  onUpdate,
  updateState,
  updateTail,
  onFreeImage,
  onStartImageService,
  onStopImageService,
}: {
  surface: OnBoxSurface;
  onSurface: (s: OnBoxSurface) => void;
  hostingEnabled: boolean;
  models: LocalModelInfo[];
  hostMemory: { total_gb: number; used_gb: number } | null;
  image: ImageSettings | null;
  busy: Set<string>;
  onUnloadToStaged: (id: string) => void;
  onLoad: (id: string) => void;
  onStage: (id: string, on: boolean) => void;
  onSetWindow: (id: string, window: number | null) => void;
  onInstall: (id: string, on: boolean) => void;
  onUninstall: (id: string, on: boolean) => void;
  onUpdate: () => void;
  updateState: "idle" | "running" | "failed";
  updateTail: string;
  onFreeImage: () => void;
  onStartImageService: () => void;
  onStopImageService: () => void;
}) {
  const enabled = models.filter((m) => m.enabled);
  const loaded = enabled.filter((m) => m.loaded);
  const stagedOnly = enabled.filter((m) => m.staged && !m.loaded);
  const unloaded = enabled.filter((m) => !m.loaded && !m.staged);

  // Catalog (library surface): everything in the roster. Un-provisioned ones get an
  // Install row; provisioned ones get an Uninstall row. Queued installs + pending
  // removals both reach the supervisor update one-shot via the queue bar.
  const available = hostingEnabled ? models.filter((m) => !m.enabled) : [];
  const queued = available.filter((m) => m.queued);
  const queuedGb = queued.reduce((sum, m) => sum + m.size_gb, 0);
  const removing = hostingEnabled ? enabled.filter((m) => m.remove_queued) : [];

  // The image service's live VRAM draw shares the pool. ComfyUI has no per-model
  // "loaded" flag, so a non-trivial draw stands in for "a model is resident".
  const imgMem = image?.memory ?? null;
  const imgUsedGb = imgMem ? Math.max(imgMem.total_gb - imgMem.free_gb, 0) : 0;
  const imgReachable = image?.reachable ?? false;
  const imgActive = imgReachable && imgUsedGb > IMG_ACTIVE_GB;
  // The resident image model heuristic: the enabled one, else the first.
  const imgModels = image?.models ?? [];
  const enabledImg = imgModels.filter((m) => m.enabled);
  const residentImg = imgActive ? (enabledImg[0] ?? imgModels[0] ?? null) : null;

  // Resident / staged LLM footprints feeding the gauge + lane subtotals.
  const residentGb = loaded.reduce((sum, m) => sum + residentGbOf(m), 0);
  const stagedGb = stagedOnly.reduce((sum, m) => sum + residentGbOf(m), 0);
  // The single unified pool: prefer the host gauge, fall back to image VRAM total.
  const total = hostMemory?.total_gb ?? imgMem?.total_gb ?? 0;
  const usedGb = residentGb + imgUsedGb;
  const projectedGb = usedGb + stagedGb;
  const freeGb = Math.max(total - usedGb, 0);
  const over = total > 0 && projectedGb > total;

  // The residency lanes: image models ride the same rungs (read-only), violet.
  // RESIDENT carries loaded LLMs + the resident image model; UNLOADED carries
  // unloaded LLMs + every enabled image model that isn't the resident one.
  const residentImgLane = residentImg ? [residentImg] : [];
  const unloadedImg = enabledImg.filter((m) => m.id !== residentImg?.id);

  const anyResident = loaded.length + residentImgLane.length;
  const meterShown = total > 0 && (anyResident > 0 || stagedOnly.length > 0);

  // Library badge: how much of the catalog is installed.
  const catBadge = `${enabled.length}/${models.length}`;

  function gaugeSegs() {
    const segs: { key: string; name: string; gb: number; style: CSSProperties }[] = [];
    let slot = 0;
    for (const m of loaded) {
      const gb = residentGbOf(m);
      segs.push({
        key: m.id,
        name: firstWord(m.label),
        gb,
        style: { width: `${(gb / total) * 100}%`, background: slotGradient(slot++) },
      });
    }
    if (residentImg) {
      const gb = imgUsedGb || imgFootGb(residentImg);
      segs.push({
        key: residentImg.id,
        name: firstWord(residentImg.label),
        gb,
        style: { width: `${(gb / total) * 100}%`, background: "var(--violet)" },
      });
    }
    return segs;
  }
  function stagedSegs() {
    let slot = loaded.length;
    return stagedOnly.map((m) => {
      const gb = residentGbOf(m);
      return {
        key: m.id,
        name: firstWord(m.label),
        gb,
        style: { width: `${(gb / total) * 100}%`, background: slotGradient(slot++) },
      };
    });
  }

  return (
    <section className="onbox-card" aria-label="On-box models">
      {/* ===== capacity gauge — the honest "what fits" signal ===== */}
      <div className="gauge">
        <div className="gauge-top">
          <span className="gauge-ttl">on-box memory</span>
          {meterShown && (
            <span className={`gauge-free${over ? " over" : freeGb < total * 0.1 ? " warn" : ""}`}>
              {over
                ? `${Math.round(projectedGb - total)} GB over if staged loads`
                : `${Math.round(freeGb)} GB free`}
            </span>
          )}
        </div>
        {meterShown ? (
          <>
            <div className="gauge-bar" aria-label="unified memory in use">
              {gaugeSegs().map((s) => (
                <div key={s.key} className="gseg" style={s.style}>
                  <span className="gseg-lab">
                    {s.name} {Math.round(s.gb)}G
                  </span>
                </div>
              ))}
              {stagedSegs().map((s) => (
                <div key={s.key} className="gseg staged" style={s.style}>
                  <span className="gseg-lab">
                    {s.name} {Math.round(s.gb)}G
                  </span>
                </div>
              ))}
              {over && <span className="gauge-over-rail" aria-hidden="true" />}
            </div>
            <div className="gauge-cap">
              <span>{Math.round(usedGb)} GB resident</span>
              {imgActive && (
                <span className="gauge-key">
                  <span className="gauge-sw" />
                  image {Math.round(imgUsedGb)} GB
                </span>
              )}
              {stagedGb > 0.5 && (
                <span className={over ? "over" : "warn"}>
                  +{Math.round(stagedGb)} GB staged → {Math.round(projectedGb)} GB
                  {over ? " over" : ""}
                </span>
              )}
              <span className="gauge-total">{Math.round(total)} GB total</span>
            </div>
          </>
        ) : (
          <p className="gauge-empty">
            nothing resident — stage a model to warm it, then load it up into memory.
          </p>
        )}
      </div>

      {/* ===== surface switch: residency vs library ===== */}
      <div className="surf-row seg-row" role="tablist" aria-label="On-box surface">
        <button
          type="button"
          role="tab"
          aria-selected={surface === "lanes"}
          className={`surf seg${surface === "lanes" ? " seg-on" : ""}`}
          onClick={() => onSurface("lanes")}
        >
          <span className="seg-ic">{SurfaceIcon.lanes}</span>
          residency
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={surface === "catalog"}
          className={`surf seg${surface === "catalog" ? " seg-on" : ""}`}
          onClick={() => onSurface("catalog")}
        >
          <span className="seg-ic">{SurfaceIcon.catalog}</span>
          library <span className="surf-badge">{hostingEnabled ? catBadge : ""}</span>
        </button>
      </div>

      {/* ===== residency (ladder) surface ===== */}
      {surface === "lanes" && (
        <div className="onbox-surface">
          {!hostingEnabled ? (
            <p className="llm-local-hint onbox-gutter">
              Self-hosting is off. Provision on the server with{" "}
              <code>jbrain enable-local-models</code>; models you enable there become selectable in
              the tiers above.
            </p>
          ) : (
            <>
              <div className="ladder-hint">
                <span className="ladder-rungs" aria-hidden="true">
                  <span>▲</span>
                  <span>│</span>
                  <span>▼</span>
                </span>
                <span>
                  the ladder runs resident → staged → unloaded. you move a model one rung at a time
                  — never skipping staged. ▲ promotes one rung, ▼ demotes one rung.
                </span>
              </div>

              {image && (
                <ImageServiceRow
                  image={image}
                  onFree={onFreeImage}
                  onStart={onStartImageService}
                  onStop={onStopImageService}
                />
              )}

              <div className="lanes">
                <Lane
                  rung="resident"
                  name="RESIDENT"
                  sub="ceiling · in memory, serving"
                  subtotal={`${Math.round(residentGb + imgUsedGb)} / ${Math.round(total)} GB`}
                  empty={
                    anyResident
                      ? null
                      : "nothing resident — load a staged model up one rung into memory."
                  }
                >
                  {loaded.map((m) => (
                    <LlmTile
                      key={m.id}
                      model={m}
                      rung="resident"
                      total={total}
                      busy={busy.has(m.id)}
                      onUnloadToStaged={onUnloadToStaged}
                      onLoad={onLoad}
                      onStage={onStage}
                      onSetWindow={onSetWindow}
                    />
                  ))}
                  {residentImgLane.map((m) => (
                    <ImageTile
                      key={m.id}
                      model={m}
                      rung="resident"
                      total={total}
                      vramGb={imgUsedGb || imgFootGb(m)}
                      serviceRunning={imgReachable}
                      onFree={onFreeImage}
                    />
                  ))}
                </Lane>

                <Lane
                  rung="staged"
                  name="STAGED"
                  sub="warm · skips the cold load"
                  subtotal={stagedOnly.length ? `+${Math.round(stagedGb)} GB projected` : "—"}
                  over={over && stagedOnly.length > 0}
                  empty={
                    stagedOnly.length
                      ? null
                      : "nothing staged — staging warms weights so the next request skips the cold load. it is the only path between unloaded and resident."
                  }
                >
                  {stagedOnly.map((m) => (
                    <LlmTile
                      key={m.id}
                      model={m}
                      rung="staged"
                      total={total}
                      busy={busy.has(m.id)}
                      onUnloadToStaged={onUnloadToStaged}
                      onLoad={onLoad}
                      onStage={onStage}
                      onSetWindow={onSetWindow}
                    />
                  ))}
                </Lane>

                <Lane
                  rung="unloaded"
                  name="UNLOADED"
                  sub="floor · on device, idle"
                  subtotal=""
                  empty={
                    unloaded.length + unloadedImg.length
                      ? null
                      : "nothing unloaded — every installed model is staged or resident."
                  }
                >
                  {unloaded.map((m) => (
                    <LlmTile
                      key={m.id}
                      model={m}
                      rung="unloaded"
                      total={total}
                      busy={busy.has(m.id)}
                      onUnloadToStaged={onUnloadToStaged}
                      onLoad={onLoad}
                      onStage={onStage}
                      onSetWindow={onSetWindow}
                    />
                  ))}
                  {unloadedImg.map((m) => (
                    <ImageTile
                      key={m.id}
                      model={m}
                      rung="unloaded"
                      total={total}
                      vramGb={imgFootGb(m)}
                      serviceRunning={imgReachable}
                      onFree={onFreeImage}
                    />
                  ))}
                </Lane>
              </div>
            </>
          )}
        </div>
      )}

      {/* ===== library (catalog) surface ===== */}
      {surface === "catalog" && (
        <div className="onbox-surface">
          {!hostingEnabled ? (
            <p className="llm-local-hint onbox-gutter">
              Self-hosting is off. Provision on the server with{" "}
              <code>jbrain enable-local-models</code>; models you enable there become selectable in
              the tiers above.
            </p>
          ) : (
            <>
              <div className="cat-list">
                <p className="onbox-tab-hint">
                  Every catalog model — language and image. Install pulls the weights; Uninstall
                  removes them and frees the disk. Installed models appear on the residency ladder.
                </p>
                <div className="cat-divider llm">
                  <span className="cat-rail" aria-hidden="true" />
                  language models
                  <span className="cat-line" aria-hidden="true" />
                </div>
                {models.map((m) =>
                  m.enabled ? (
                    <UninstallRow
                      key={m.id}
                      model={m}
                      busy={busy.has(m.id)}
                      onUninstall={onUninstall}
                    />
                  ) : (
                    <InstallRow key={m.id} model={m} busy={busy.has(m.id)} onInstall={onInstall} />
                  ),
                )}
                <div className="cat-divider img">
                  <span className="cat-rail" aria-hidden="true" />
                  image models
                  <span className="cat-line" aria-hidden="true" />
                </div>
                {imgModels.length > 0 ? (
                  imgModels.map((m) => imageCatalogRow(m))
                ) : (
                  <p className="onbox-tab-hint">
                    Image catalog. Provision the weights on the box with{" "}
                    <code>comfyui-setup.sh</code>. One model is resident at a time.
                  </p>
                )}
              </div>
              {(queued.length > 0 || removing.length > 0) && (
                <div className="llm-local-queue">
                  <div className="llm-local-queue-text">
                    <b>
                      {[
                        queued.length > 0 &&
                          `${queued.length} to install · ${Math.round(queuedGb)} GB`,
                        removing.length > 0 && `${removing.length} to uninstall`,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </b>
                    <span>
                      {updateState === "running"
                        ? updateTail || "Update running — applying changes after rebuild…"
                        : updateState === "failed"
                          ? "Update failed — check the Ops screen."
                          : "Applied on your next update, or start one now."}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="llm-local-btn load"
                    disabled={updateState === "running"}
                    onClick={onUpdate}
                  >
                    {updateState === "running"
                      ? "Updating…"
                      : queued.length > 0
                        ? "Update & install now"
                        : "Update & apply now"}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}

// One ladder lane (rung): an accent rail, a dot, the rung name + one-line sub, a GB
// subtotal in the head, the tiles, and a quiet empty-state line when no tile sits here.
function Lane({
  rung,
  name,
  sub,
  subtotal,
  over,
  empty,
  children,
}: {
  rung: Rung;
  name: string;
  sub: string;
  subtotal: string;
  over?: boolean;
  empty: string | null;
  children: ReactNode;
}) {
  return (
    <section className={`lane ${rung}${over ? " over" : ""}`}>
      <div className="lane-head">
        <span className="lane-dot" aria-hidden="true" />
        <span className="lane-name">{name}</span>
        <span className="lane-sub">{sub}</span>
        {subtotal && <span className={`lane-subtotal${over ? " over" : ""}`}>{subtotal}</span>}
      </div>
      {empty ? <p className="lane-empty">{empty}</p> : <div className="lane-tiles">{children}</div>}
    </section>
  );
}

// A small motion helper: relocate the tile to the adjacent lane (≤165ms), then run
// the actual API move. prefers-reduced-motion skips the animation entirely.
function steppedMove(node: HTMLElement | null, dir: "up" | "down", run: () => void) {
  if (!node || reduceMotion()) {
    run();
    return;
  }
  node.classList.add(dir === "up" ? "relocating-up" : "relocating-down");
  window.setTimeout(run, 165);
}

// One installed LLM as a ladder tile: footprint strip, capability chips, the
// context-window control (in-memory rungs only, locked while resident), and the
// ladder stepper (exactly one rung up and/or down — never skipping staged).
function LlmTile({
  model: m,
  rung,
  total,
  busy: isBusy,
  onUnloadToStaged,
  onLoad,
  onStage,
  onSetWindow,
}: {
  model: LocalModelInfo;
  rung: Rung;
  total: number;
  busy: boolean;
  onUnloadToStaged: (id: string) => void;
  onLoad: (id: string) => void;
  onStage: (id: string, on: boolean) => void;
  onSetWindow: (id: string, window: number | null) => void;
}) {
  const tileRef = useRef<HTMLDivElement>(null);
  const weights = m.disk_gb ?? m.size_gb;
  const sizeText = `${m.disk_gb == null ? "~" : ""}${weights} GB`;
  const foot = residentGbOf(m);
  const footW = total > 0 ? (foot / total) * 100 : 0;
  const footLab = rung === "resident" ? "weights+KV" : rung === "staged" ? "warm" : "if loaded";
  const footPrefix = rung === "unloaded" ? "~" : "";

  // Context window: editable on in-memory rungs (staged), locked while resident.
  const showCtx = rung !== "unloaded";
  const editable = rung !== "resident" && !isBusy;
  const effWindow = m.context_window_override ?? m.context_window;
  const windowOpts = Array.from(
    new Set([...WINDOW_CHOICES.filter((w) => w <= m.context_window), m.context_window]),
  ).sort((a, b) => a - b);

  const step = (dir: "up" | "down", run: () => void) => steppedMove(tileRef.current, dir, run);

  return (
    <div ref={tileRef} className={`tile ${rung}`} data-id={m.id}>
      <div className="tile-foot" style={{ width: `${footW}%` }} aria-hidden="true" />
      <div className="tile-head">
        <div className="tile-name">
          {m.label}
          <span className="tile-meta">
            {m.quant} · {sizeText}
          </span>
        </div>
        <span className="tile-foot-lab">
          <b>
            {footPrefix}
            {Math.round(foot)} GB
          </b>
          <br />
          {footLab}
        </span>
      </div>
      <div className="tile-chips">
        {capabilityChips(m).map((c) => (
          <span key={c.key} className={`llm-chip llm-chip-${c.cls}`}>
            {c.label}
          </span>
        ))}
      </div>
      {m.note && <p className="tile-note">{m.note}</p>}
      {showCtx && (
        <div className="tile-ctx">
          <label className="tile-ctx-label" htmlFor={`ctx-${m.id}`}>
            context
          </label>
          <select
            id={`ctx-${m.id}`}
            className="tile-ctx-select"
            value={String(effWindow)}
            disabled={!editable}
            onChange={(e) => {
              const v = Number(e.target.value);
              onSetWindow(m.id, v === m.context_window ? null : v);
            }}
          >
            {windowOpts.map((w) => (
              <option key={w} value={w}>
                {fmtTokens(w)}
              </option>
            ))}
          </select>
          {rung === "resident" ? (
            <span className="tile-ctx-hint">🔒 unload to change</span>
          ) : (
            <span className="tile-ctx-meta">KV ~{m.kv_gb} GB</span>
          )}
        </div>
      )}
      <div className="tile-acts">
        {rung === "unloaded" && (
          <button
            type="button"
            className="tile-btn up"
            disabled={isBusy}
            onClick={() => step("up", () => onStage(m.id, true))}
          >
            <span className="dir" aria-hidden="true">
              ▲
            </span>{" "}
            {isBusy ? "…" : "stage"}
          </button>
        )}
        {rung === "staged" && (
          <>
            <button
              type="button"
              className="tile-btn up"
              disabled={isBusy}
              onClick={() => step("up", () => onLoad(m.id))}
            >
              <span className="dir" aria-hidden="true">
                ▲
              </span>{" "}
              {isBusy ? "…" : "load"}
            </button>
            <button
              type="button"
              className="tile-btn down"
              disabled={isBusy}
              onClick={() => step("down", () => onStage(m.id, false))}
            >
              <span className="dir" aria-hidden="true">
                ▼
              </span>{" "}
              unstage
            </button>
          </>
        )}
        {rung === "resident" && (
          <button
            type="button"
            className="tile-btn down"
            disabled={isBusy}
            onClick={() => step("down", () => onUnloadToStaged(m.id))}
          >
            <span className="dir" aria-hidden="true">
              ▼
            </span>{" "}
            {isBusy ? "…" : "unload"}
          </button>
        )}
      </div>
    </div>
  );
}

// An image model as a read-only ladder tile (violet). Its lifecycle is the
// service's, not per-model: a RESIDENT tile can be ▼ freed; an UNLOADED tile has no
// per-model load (it loads on first request), gated by the service running.
function ImageTile({
  model: m,
  rung,
  total,
  vramGb,
  serviceRunning,
  onFree,
}: {
  model: ImageModelInfo;
  rung: Rung;
  total: number;
  vramGb: number;
  serviceRunning: boolean;
  onFree: () => void;
}) {
  const footW = total > 0 ? (vramGb / total) * 100 : 0;
  const footLab = rung === "resident" ? "VRAM" : "if loaded";
  const footPrefix = rung === "resident" ? "" : "~";
  const sizeText = `${m.disk_gb == null ? "~" : ""}${m.disk_gb ?? m.size_gb} GB disk`;
  return (
    <div className={`tile img ${rung}`} data-id={m.id}>
      <div className="tile-foot" style={{ width: `${footW}%` }} aria-hidden="true" />
      <div className="tile-head">
        <div className="tile-name">
          {m.label}
          <span className="tile-meta">{sizeText}</span>
        </div>
        <span className="tile-foot-lab">
          <b>
            {footPrefix}
            {Math.round(vramGb)} GB
          </b>
          <br />
          {footLab}
        </span>
      </div>
      <div className="tile-chips">
        <span className={`llm-chip llm-chip-${m.kind === "edit" ? "imgedit" : "imggen"}`}>
          {m.kind === "edit" ? "edit" : "generate"}
        </span>
      </div>
      {m.note && <p className="tile-note">{m.note}</p>}
      {rung === "resident" ? (
        <div className="tile-acts">
          <button type="button" className="tile-btn down" onClick={onFree}>
            <span className="dir" aria-hidden="true">
              ▼
            </span>{" "}
            free
          </button>
        </div>
      ) : (
        <p className="tile-note gate">
          {serviceRunning
            ? "loads on first image request"
            : "image service stopped — start it to use image models"}
        </p>
      )}
    </div>
  );
}

// A provisioned model in the library: its capability chips and a danger "Uninstall"
// (queues the removal). Once queued it reads "uninstalling" and offers "Keep".
function UninstallRow({
  model: m,
  busy,
  onUninstall,
}: {
  model: LocalModelInfo;
  busy: boolean;
  onUninstall: (id: string, on: boolean) => void;
}) {
  const footprint = m.disk_gb ?? m.size_gb;
  const sizeText = `${m.disk_gb == null ? "~" : ""}${footprint} GB`;
  const state = m.loaded ? "loaded" : m.staged ? "staged" : "idle";
  return (
    <div className={`llm-local-row on ${state}${m.remove_queued ? " removing" : ""}`}>
      <div className="llm-local-head">
        <div className="llm-local-name">
          {m.label}
          <span className="llm-local-meta">
            {m.quant} · {sizeText}
          </span>
        </div>
        <div className="llm-local-topright">
          <div className="llm-local-act">
            {m.remove_queued ? (
              <button
                type="button"
                className="llm-local-btn"
                disabled={busy}
                onClick={() => onUninstall(m.id, false)}
              >
                {busy ? "…" : "Keep"}
              </button>
            ) : (
              <button
                type="button"
                className="llm-local-btn danger"
                disabled={busy}
                onClick={() => onUninstall(m.id, true)}
              >
                {busy ? "…" : "Uninstall"}
              </button>
            )}
          </div>
          <span className={`llm-local-state${m.remove_queued ? " removing" : ""}`}>
            {m.remove_queued ? "uninstalling" : state}
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
    </div>
  );
}

// An un-provisioned catalog model in the library: capabilities, an Install/Remove
// toggle for the install queue, and — once queued and downloading — a live progress
// bar (download_gb / size_gb) the snapshot poll drives.
function InstallRow({
  model,
  busy,
  onInstall,
}: {
  model: LocalModelInfo;
  busy: boolean;
  onInstall: (id: string, on: boolean) => void;
}) {
  const downloading = model.queued && model.download_gb != null;
  const pct = downloading
    ? Math.min(100, Math.round(((model.download_gb ?? 0) / model.size_gb) * 100))
    : 0;
  return (
    <div className={`llm-local-row install${model.queued ? " queued" : ""}`}>
      <div className="llm-local-head">
        <div className="llm-local-name">
          {model.label}
          <span className="llm-local-meta">
            {model.quant} · ~{model.size_gb} GB
          </span>
        </div>
        <div className="llm-local-topright">
          <div className="llm-local-act">
            <button
              type="button"
              className={`llm-local-btn${model.queued ? "" : " load"}`}
              disabled={busy}
              onClick={() => onInstall(model.id, !model.queued)}
            >
              {busy ? "…" : model.queued ? "Remove" : "Install"}
            </button>
          </div>
          {model.queued && <span className="llm-local-state queued">queued</span>}
        </div>
      </div>
      <div className="llm-local-chips">
        {capabilityChips(model).map((c) => (
          <span key={c.key} className={`llm-chip llm-chip-${c.cls}`}>
            {c.label}
          </span>
        ))}
      </div>
      {model.note && <p className="llm-local-note">{model.note}</p>}
      {downloading && (
        <div className="llm-local-dl">
          <div className="llm-local-dl-bar">
            <i style={{ width: `${pct}%` }} />
          </div>
          <span className="llm-local-dl-cap">
            {model.download_gb} / {model.size_gb} GB · {pct}%
          </span>
        </div>
      )}
    </div>
  );
}

// The image service control row (calm): ComfyUI's running/stopped/off state plus its
// Start / Stop / Free controls. Sits above the lanes; provisioning the weights stays
// the on-box comfyui-setup.sh step.
function ImageServiceRow({
  image,
  onFree,
  onStart,
  onStop,
}: {
  image: ImageSettings;
  onFree: () => void;
  onStart: () => void;
  onStop: () => void;
}) {
  const state = image.reachable ? "running" : image.enabled ? "stopped" : "off";
  return (
    <div className={`onbox-svc llm-img llm-img-${state}`}>
      <div className="llm-img-head">
        <span className="llm-img-title">Image · ComfyUI</span>
        <span className={`llm-img-state ${state}`}>{state}</span>
        <div className="llm-img-acts">
          {image.reachable ? (
            <>
              <button type="button" className="llm-local-btn" onClick={onFree}>
                Free
              </button>
              <button type="button" className="llm-local-btn danger" onClick={onStop}>
                Stop
              </button>
            </>
          ) : image.enabled ? (
            <button type="button" className="llm-local-btn load" onClick={onStart}>
              Start
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// One image model in the library list: informational (no install button — image
// provisioning is the on-box comfyui-setup.sh step).
function imageCatalogRow(m: ImageModelInfo) {
  const footprint = m.disk_gb ?? m.size_gb;
  const sizeText = `${m.disk_gb == null ? "~" : ""}${footprint} GB`;
  return (
    <div key={m.id} className="llm-img-row">
      <div className="llm-img-name">
        {m.label}
        <span className={`llm-chip llm-chip-${m.kind === "edit" ? "imgedit" : "imggen"}`}>
          {m.kind}
        </span>
      </div>
      <div className="llm-img-meta">
        {sizeText} · {m.disk_gb != null ? "provisioned" : "not provisioned"}
        {m.disk_gb != null ? ` · ~${m.vram_gb} GB resident` : ""}
      </div>
    </div>
  );
}
