import { useEffect, useMemo, useRef, useState } from "react";
import type {
  ImageModelInfo,
  ImageSettings,
  JcodeModelInfo,
  LlmProviderId,
  LlmSettings,
  LlmTask,
  LoadPlan,
  LocalModelInfo,
  ReasoningEffort,
} from "../api/client";
import { ApiError, api } from "../api/client";
import { useForeground } from "../visibility";
import { AiUsageCard } from "./aiUsage";

// Strategy C — tasks are tiered by role. The grouping lives in the frontend
// (the wire is a flat task list); any task the API returns outside these
// tiers lands in a synthesized "Other" group so nothing is silently dropped.
interface GroupDef {
  key: string;
  /** Accent class flips the group's left rail (docs/reference/DESIGN.md accents). */
  accent: "high" | "light" | "vision";
  name: string;
  desc: string;
  taskIds: string[];
}

// Groups are the reasoning-effort buckets (backend `TASK_REASONING_BUCKET`): each
// bucket's default is correct for every task in it, so the box is right by default
// and a per-task override reads as a deliberate deviation (the card shows "mixed").
// High = async, reasoning-bound, correctness-critical (the knowledge-graph arbiters:
// integrate.note, fact.adjudicate, the Phase-6 wiki.ground verifier, and the wiki_lint
// contradiction/stale verifiers). Low =
// deterministic one-shots (entity.disambiguate/session.title/triage.classify).
// Medium = everything else that thinks (agent.turn, the extractors, video.summarize,
// wiki.rewrite, intake.materialize). Vision has no reasoning channel. Keep this in
// sync with the backend map; a task the API returns outside these defs lands in a
// synthesized "Other" group, so new routable tasks are never silently dropped.
const GROUP_DEFS: GroupDef[] = [
  {
    key: "high",
    accent: "high",
    name: "High reasoning",
    desc: "Async, correctness-critical judgment — worth the deepest thinking.",
    taskIds: [
      "integrate.note",
      "fact.adjudicate",
      "wiki.ground",
      "wiki.lint.contradiction",
      "wiki.lint.stale",
    ],
  },
  {
    key: "medium",
    accent: "high",
    name: "Medium reasoning",
    desc: "The default — interactive turns, extraction, and summaries.",
    taskIds: [
      "agent.turn",
      "note.extract",
      "correction_note.extract",
      "video.summarize",
      "wiki.rewrite",
      "intake.materialize",
    ],
  },
  {
    key: "low",
    accent: "light",
    name: "Low reasoning",
    desc: "Cheap, frequent one-shots — classify & extract.",
    taskIds: ["entity.disambiguate", "triage.classify"],
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
  // The On-box card's two independently-collapsible sections + their omnibox tabs.
  // LLMs open by default (the common case); the image section opens on demand.
  const [llmOpen, setLlmOpen] = useState(true);
  const [imgOpen, setImgOpen] = useState(false);
  // Reversed order (live first, broadest last): Resident · Available · Catalogue. The
  // Available tab is the default — it's where staging (the load preview) lives.
  const [llmTab, setLlmTab] = useState<LlmTab>("available");
  const [imgTab, setImgTab] = useState<ImgTab>("installed");
  // The transient stage preview: the model whose load we're previewing, and the server's
  // dry-run eviction plan for it. Staging is no longer a stored state — it lives only here
  // until the operator commits (Load) or cancels.
  const [stagedId, setStagedId] = useState<string | null>(null);
  const [plan, setPlan] = useState<LoadPlan | null>(null);
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
    // Poll while either section is open (the shared meter is always visible, but a
    // collapsed card needs no live refresh).
    if ((!llmOpen && !imgOpen) || !foreground || (!hostingEnabled && !imageEnabled)) return;
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
  }, [llmOpen, imgOpen, hostingEnabled, imageEnabled, foreground]);

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

  function clearPreview() {
    setStagedId(null);
    setPlan(null);
  }

  function unloadModel(id: string) {
    mark(id);
    // Unloading changes what's resident, so any open preview is now stale.
    if (stagedId !== null) clearPreview();
    api
      .unloadLocalModel(id)
      .then(reconcileLoaded)
      .catch(() => {})
      .finally(() => unmark(id));
  }

  // Commit a staged load: the endpoint evicts to fit (exactly what the preview showed) then
  // warms the model. Reconcile the resident set and clear the preview.
  function loadModel(id: string) {
    mark(id);
    api
      .loadLocalModel(id)
      .then((res) => {
        reconcileLoaded(res);
        clearPreview();
      })
      .catch(() => {})
      .finally(() => unmark(id));
  }

  // Stage = preview the load: ask the server what loading this model would evict right now,
  // and hold it as the transient preview. No side effects until the operator commits.
  function previewStage(id: string) {
    mark(id);
    api
      .planLoadLocalModel(id)
      .then((p) => {
        setStagedId(id);
        setPlan(p);
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

  // Set the model's llama-server slot count (1 or 2). A second slot dedicates an interactive
  // keep-warm slot so a primed chat prefix survives background/title traffic.
  function setParallelSlots(id: string, slots: number | null) {
    mark(id);
    const seq = ++putSeq.current;
    api
      .setLocalParallelSlots(id, slots)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark(id));
  }

  // Set (or clear, with null) the model's --image-min-tokens floor: how much of an image the
  // model actually gets to see. The knob for small text in a photo.
  function setImageMinTokens(id: string, tokens: number | null) {
    mark(id);
    const seq = ++putSeq.current;
    api
      .setLocalImageMinTokens(id, tokens)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark(id));
  }

  // Set (or clear, with null) the box-wide residency free-RAM floor. Keyed on a fixed busy
  // id (not a model id) since it's a global knob, not a per-row action.
  function setFreeRam(fraction: number | null) {
    mark("free-ram");
    const seq = ++putSeq.current;
    api
      .setFreeRamFraction(fraction)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark("free-ram"));
  }

  // Turn the end-of-turn restore on or off. A box-wide knob like the free-RAM floor, so it
  // gets its own fixed busy id rather than a model id.
  function setAutoRestore(enabled: boolean) {
    mark("auto-restore");
    const seq = ++putSeq.current;
    api
      .setAutoRestore(enabled)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark("auto-restore"));
  }

  // Toggle a provisioned model available/unavailable to the router (keeps the weights). A
  // model made unavailable can't be staged, so drop any open preview of it.
  function setAvailable(id: string, on: boolean) {
    mark(id);
    if (!on && stagedId === id) clearPreview();
    const seq = ++putSeq.current;
    api
      .setLocalAvailable(id, on)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark(id));
  }

  // Choose the model the code-mode (jcode) agent runs; "" reverts to the default.
  // Returns the full snapshot (guarded reconcile, like stage/context-window).
  function setJcodeModel(model: string) {
    mark("jcode-model");
    const seq = ++putSeq.current;
    api
      .setJcodeModel(model)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark("jcode-model"));
  }

  // Choose the planner model for code mode's grok `plan` subagent; "" reverts to the
  // config split default, the `planner_same` sentinel collapses the card to a single model.
  function setJcodePlanner(planner: string) {
    mark("jcode-model");
    const seq = ++putSeq.current;
    api
      .setJcodePlanner(planner)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark("jcode-model"));
  }

  // Queue a model for install, then start its download immediately — no system
  // update. The snapshot reflects the queued flag at once and the per-model bar fills
  // as the sync one-shot pulls the weights. Un-queueing (on=false) just clears it.
  function queueInstall(id: string, on: boolean) {
    mark(id);
    const seq = ++putSeq.current;
    api
      .queueLocalInstall(id, on)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
        if (on) startDownload();
      })
      .catch(() => {})
      .finally(() => unmark(id));
  }

  // Queue a provisioned model for uninstall, then apply it now via the same sync
  // one-shot — destructive (it drops the model from LOCAL_MODELS and prunes its
  // weights). The tap-again confirm lives in the button (ConfirmButton), so this just
  // performs the queued action.
  function queueUninstall(id: string, on: boolean) {
    mark(id);
    const seq = ++putSeq.current;
    api
      .queueLocalUninstall(id, on)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
        if (on) startDownload();
      })
      .catch(() => {})
      .finally(() => unmark(id));
  }

  // "Download": kick the supervisor's local-model sync one-shot, which downloads the
  // queued weights (and applies queued removals) WITHOUT a system update — installing
  // a model no longer rides a full server update. We follow it two ways: the coarse
  // phase from the sync log tail (here, which surfaces the verbose failure reason),
  // and each model's live download bar (the snapshot poll).
  const [downloadState, setDownloadState] = useState<"idle" | "running" | "failed">("idle");
  const [downloadTail, setDownloadTail] = useState("");
  function startDownload() {
    setDownloadState("running");
    setDownloadTail("");
    api.opsLocalProvisionStart().catch((e) => {
      // 409 = a one-shot is already running; attach to it (the poll shows its state)
      // rather than reporting a spurious failure.
      if (e instanceof ApiError && e.status === 409) return;
      setDownloadState("failed");
    });
  }
  useEffect(() => {
    if (downloadState !== "running" || !foreground) return;
    let stop = false;
    const tick = () => {
      api
        .opsLocalProvisionStatus()
        .then((s) => {
          if (stop) return;
          const lines = s.log_tail.trimEnd().split("\n");
          setDownloadTail(lines[lines.length - 1] ?? "");
          // The download runs inside the one-shot, so an exit means the sync is done
          // (the snapshot poll has the models enabled by now); a non-zero exit surfaces
          // the failure, its reason in the last log line.
          if (s.state === "exited") setDownloadState(s.exit_code === 0 ? "idle" : "failed");
        })
        .catch(() => {});
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [downloadState, foreground]);

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
        llmOpen={llmOpen}
        imgOpen={imgOpen}
        onToggleLlm={() => setLlmOpen((v) => !v)}
        onToggleImg={() => setImgOpen((v) => !v)}
        llmTab={llmTab}
        imgTab={imgTab}
        onLlmTab={setLlmTab}
        onImgTab={setImgTab}
        hostingEnabled={settings.local_hosting_enabled}
        models={settings.local_models}
        hostMemory={settings.host_memory}
        freeRam={settings.free_ram}
        onSetFreeRam={setFreeRam}
        freeRamBusy={busy.has("free-ram")}
        autoRestore={settings.auto_restore}
        onSetAutoRestore={setAutoRestore}
        autoRestoreBusy={busy.has("auto-restore")}
        image={image}
        busy={busy}
        stagedId={stagedId}
        plan={plan}
        onUnload={unloadModel}
        onLoad={loadModel}
        onStage={previewStage}
        onCancelStage={clearPreview}
        onSetWindow={setContextWindow}
        onSetSlots={setParallelSlots}
        onSetImageFloor={setImageMinTokens}
        onSetAvailable={setAvailable}
        onInstall={queueInstall}
        onUninstall={queueUninstall}
        onDownload={startDownload}
        downloadState={downloadState}
        downloadTail={downloadTail}
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

      {/* Code mode sits at the bottom, under the role tiers (it's a single-model
          choice, not a per-task tier), styled to match them. */}
      {settings.jcode.enabled && (
        <JcodeModelCard
          jcode={settings.jcode}
          busy={busy.has("jcode-model")}
          onChange={setJcodeModel}
          onPlannerChange={setJcodePlanner}
        />
      )}

      <AiUsageCard />
    </main>
  );
}

// The code-mode (jcode) agent's model selectors. Rendered only when code mode is
// enabled; both dropdowns list installed, tool-capable local models (the API's
// `jcode.options`). The card splits the agent into two roles: the EXECUTOR (grok's
// default model — the coder) and the PLANNER (grok's `plan` subagent — a reasoner).
// The planner also offers "Same as executor" to collapse the card to a single model.
function JcodeModelCard({
  jcode,
  busy,
  onChange,
  onPlannerChange,
}: {
  jcode: JcodeModelInfo;
  busy: boolean;
  onChange: (model: string) => void;
  onPlannerChange: (planner: string) => void;
}) {
  // An effective selection may not be among the installed options (e.g. the config
  // default before its weights are installed) — surface it as a disabled option so the
  // select shows the truth instead of silently snapping to another model.
  const execMissing = !!jcode.model && !jcode.options.some((o) => o.id === jcode.model);
  const single = jcode.planner === jcode.planner_same;
  const plannerMissing =
    !single && !!jcode.planner && !jcode.options.some((o) => o.id === jcode.planner);
  const hasChoices = jcode.options.length > 0 || execMissing;
  return (
    <section className="llm-group llm-jcode" aria-label="Code mode model card">
      <div className="llm-group-head">
        <div className="llm-group-title">
          <span className="llm-group-name">Code mode</span>
          <span className="llm-group-count">2 roles</span>
        </div>
        <p className="llm-group-desc">
          The local models the jcode coding agent runs — the executor writes code, the planner
          drives grok’s <code>plan</code> subagent. Pick “Same as executor” to run one model for
          both. New sessions use the current choice; an in-flight session keeps what it started
          with.
        </p>

        {hasChoices ? (
          <>
            <span className="llm-field-tag">Executor</span>
            <select
              className="llm-select"
              aria-label="Code mode executor model"
              value={jcode.model}
              disabled={busy}
              onChange={(e) => onChange(e.target.value)}
            >
              {execMissing && (
                <option value={jcode.model} disabled>
                  {jcode.model} (not installed)
                </option>
              )}
              {jcode.options.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                  {o.id === jcode.default ? " · default" : ""}
                </option>
              ))}
            </select>

            <span className="llm-field-tag">Planner</span>
            <select
              className="llm-select"
              aria-label="Code mode planner model"
              value={single ? jcode.planner_same : jcode.planner}
              disabled={busy}
              onChange={(e) => onPlannerChange(e.target.value)}
            >
              <option value={jcode.planner_same}>Same as executor (single model)</option>
              {plannerMissing && (
                <option value={jcode.planner} disabled>
                  {jcode.planner} (not installed)
                </option>
              )}
              {jcode.options.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                  {o.id === jcode.planner_default ? " · suggested" : ""}
                </option>
              ))}
            </select>
          </>
        ) : (
          <p className="llm-na-note">
            Install a tool-capable local model (above) to choose one for code mode.
          </p>
        )}
      </div>
    </section>
  );
}

// A local patch shape: either field may be absent (a provider-only or
// reasoning-only change). applyTasks reconciles the omitted field from state.
interface LlmTaskPatchLocal {
  provider?: LlmProviderId;
  reasoning_effort?: ReasoningEffort;
}

// Capability chips for a local model — same muted register as the rest of the
// chrome (docs/reference/DESIGN.md), keyed by what the model can do.
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

// The image model's bar segment — a distinct violet (the "generate" accent), so it
// reads apart from the LLM slot palette (greens/oranges) on the shared meter.
const IMG_GRADIENT = "linear-gradient(90deg, hsl(265 35% 64%), hsl(284 35% 64%))";
// ComfyUI reports VRAM, not a per-model load flag; treat a draw above this as "a
// model is resident" so the bar shows it. An estimate — tune on the box.
const IMG_ACTIVE_GB = 4;

// The size picker's choices, capped per model at its catalog window. The 500k/1M
// steps only surface for models whose native window reaches them (e.g. Llama 4 Scout).
const WINDOW_CHOICES = [16384, 32768, 65536, 131072, 196608, 262144, 500000, 1000000];
const fmtTokens = (n: number) => {
  if (n >= 1_000_000) return `${Math.round(n / 100_000) / 10}M`;
  return n % 1024 === 0 ? `${n / 1024}k` : `${Math.round(n / 1000)}k`;
};
const barName = (m: LocalModelInfo) => m.label.split(" ")[0];
const residentGbOf = (m: LocalModelInfo) => (m.disk_gb ?? m.size_gb) + m.kv_gb;

// The On-box LLM section's omnibox tabs (reversed order: Resident · Available · Catalogue)
// and the image section's; both reuse the shared `.seg-row` segmented control, accented per
// section (steel / violet).
type LlmTab = "resident" | "available" | "catalogue";
type ImgTab = "installed" | "catalog";

// Small inline glyphs for the omnibox tabs: a memory chip for resident, swap arrows for
// available, a check for installed (image), a grid for the catalog(ue).
const TabIcon = {
  resident: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="M7 9v6M11 9v6M15 12h2" />
    </svg>
  ),
  available: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      <path d="M4 8h13M14 5l3 3-3 3" />
      <path d="M20 16H7M10 13l-3 3 3 3" />
    </svg>
  ),
  installed: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  ),
  catalog: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </svg>
  ),
} as const;

type TabIconKey = keyof typeof TabIcon;

// The omnibox segmented tabs (reuses `.seg-row`/`.seg`/`.seg-on`). The active
// segment is a clean tint fill — the parent section sets `--mode`/`--mode-tint`,
// and these tabs deliberately do NOT live under `.llm-group`, so they never inherit
// its inset ring.
function OmniTabs<T extends string>({
  label,
  tabs,
  active,
  onTab,
}: {
  label: string;
  tabs: { id: T; label: string; icon: TabIconKey }[];
  active: T;
  onTab: (tab: T) => void;
}) {
  return (
    <div className="seg-row onbox-tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={active === t.id}
          className={`seg${active === t.id ? " seg-on" : ""}`}
          onClick={() => onTab(t.id)}
        >
          <span className="seg-ic">{TabIcon[t.icon]}</span>
          {t.label}
        </button>
      ))}
    </div>
  );
}

// One section header in the card: an accent rail, its name, a live meta count, and
// a caret that rotates open. The two sections share this chrome (steel / violet).
function SectionToggle({
  accent,
  name,
  meta,
  open,
  onToggle,
}: {
  accent: "llm" | "img";
  name: string;
  meta: string;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className={`onbox-sec-toggle ${accent}${open ? " open" : ""}`}
      aria-expanded={open}
      onClick={onToggle}
    >
      <span className="onbox-rail" aria-hidden="true" />
      <span className="onbox-sec-name">{name}</span>
      <span className="onbox-sec-meta">{meta}</span>
      <span className={`llm-exp-caret${open ? " llm-exp-open" : ""}`} aria-hidden="true">
        ›
      </span>
    </button>
  );
}

// Presets for the residency free-RAM floor (fraction kept free). 12.5% / 15% are the tuned
// runbook values; the wider band lets a box trade co-residence room against freeze safety.
const FREE_RAM_PRESETS = [0.05, 0.1, 0.125, 0.15, 0.2, 0.25, 0.3];
const freeRamPct = (f: number) => `${+(f * 100).toFixed(1)}%`;

// The box-wide free-RAM headroom control shown under the memory meter (hosting on only). The
// evictor keeps this fraction of RAM free before every local load; lowering it lets more
// models co-reside, raising it keeps more margin against the kernel-reclaim freeze.
function FreeRamControl({
  freeRam,
  busy,
  onChange,
}: {
  freeRam: { fraction: number; default: number; override: number | null };
  busy: boolean;
  onChange: (fraction: number | null) => void;
}) {
  // Always fold in the effective + default values so the current one is selectable even if
  // it isn't a preset (e.g. a hand-set env value).
  const options = Array.from(
    new Set([...FREE_RAM_PRESETS, freeRam.fraction, freeRam.default]),
  ).sort((a, b) => a - b);
  return (
    <div className="onbox-freeram">
      <label className="onbox-freeram-label" htmlFor="free-ram-select">
        Free-RAM headroom
      </label>
      <select
        id="free-ram-select"
        className="onbox-freeram-select"
        value={String(freeRam.fraction)}
        disabled={busy}
        onChange={(e) => onChange(Number(e.target.value))}
      >
        {options.map((f) => (
          <option key={f} value={f}>
            {freeRamPct(f)}
            {f === freeRam.default ? " · default" : ""}
          </option>
        ))}
      </select>
      {freeRam.override !== null && (
        <button
          type="button"
          className="onbox-freeram-reset"
          disabled={busy}
          onClick={() => onChange(null)}
        >
          Reset
        </button>
      )}
      <span className="onbox-freeram-hint">
        kept free before each model load — lower to co-reside more, raise for freeze safety
      </span>
    </div>
  );
}

// The end-of-turn restore switch, shown beside the free-RAM floor (hosting on only). ON is
// the long-standing behaviour: after a displacement (an image render, a code session) the box
// puts the displaced models back when the turn ends, so it settles at a steady state instead
// of cold-loading next turn. OFF stops that — nothing loads until a turn needs it, which is
// what you want while diagnosing the box. It is a SURPRISE control, not a safety one: every
// load is guarded against device memory either way.
function AutoRestoreControl({
  enabled,
  busy,
  onChange,
}: {
  enabled: boolean;
  busy: boolean;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <div className="onbox-freeram">
      <label className="onbox-freeram-label" htmlFor="auto-restore-toggle">
        Auto-restore models
      </label>
      <input
        id="auto-restore-toggle"
        type="checkbox"
        className="onbox-autorestore-toggle"
        checked={enabled}
        disabled={busy}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="onbox-freeram-hint">
        {enabled
          ? "displaced models come back when a turn ends"
          : "off — models load only when a turn needs one"}
      </span>
    </div>
  );
}

// One card, a shared always-visible unified-memory meter, then two independently
// collapsible sections (On-box LLMs / Image models). The meter is fed BOTH the LLM
// (loaded + staged) and the image-VRAM segments since the box shares one RAM pool.
function OnBoxModelsCard({
  llmOpen,
  imgOpen,
  onToggleLlm,
  onToggleImg,
  llmTab,
  imgTab,
  onLlmTab,
  onImgTab,
  hostingEnabled,
  models,
  hostMemory,
  freeRam,
  onSetFreeRam,
  freeRamBusy,
  autoRestore,
  onSetAutoRestore,
  autoRestoreBusy,
  image,
  busy,
  stagedId,
  plan,
  onUnload,
  onLoad,
  onStage,
  onCancelStage,
  onSetWindow,
  onSetSlots,
  onSetImageFloor,
  onSetAvailable,
  onInstall,
  onUninstall,
  onDownload,
  downloadState,
  downloadTail,
  onFreeImage,
  onStartImageService,
  onStopImageService,
}: {
  llmOpen: boolean;
  imgOpen: boolean;
  onToggleLlm: () => void;
  onToggleImg: () => void;
  llmTab: LlmTab;
  imgTab: ImgTab;
  onLlmTab: (tab: LlmTab) => void;
  onImgTab: (tab: ImgTab) => void;
  hostingEnabled: boolean;
  models: LocalModelInfo[];
  hostMemory: { total_gb: number; used_gb: number } | null;
  freeRam: { fraction: number; default: number; override: number | null };
  onSetFreeRam: (fraction: number | null) => void;
  freeRamBusy: boolean;
  autoRestore: boolean;
  onSetAutoRestore: (enabled: boolean) => void;
  autoRestoreBusy: boolean;
  image: ImageSettings | null;
  busy: Set<string>;
  stagedId: string | null;
  plan: LoadPlan | null;
  onUnload: (id: string) => void;
  onLoad: (id: string) => void;
  onStage: (id: string) => void;
  onCancelStage: () => void;
  onSetWindow: (id: string, window: number | null) => void;
  onSetSlots: (id: string, slots: number | null) => void;
  onSetImageFloor: (id: string, tokens: number | null) => void;
  onSetAvailable: (id: string, on: boolean) => void;
  onInstall: (id: string, on: boolean) => void;
  onUninstall: (id: string, on: boolean) => void;
  onDownload: () => void;
  downloadState: "idle" | "running" | "failed";
  downloadTail: string;
  onFreeImage: () => void;
  onStartImageService: () => void;
  onStopImageService: () => void;
}) {
  // "Available" is the router's swap roster (effective-available, m.available); "resident"
  // is loaded now. `enabled` (provisioned-on-box) still drives the Catalogue install rows.
  const available = models.filter((m) => m.available);
  const loaded = models.filter((m) => m.loaded);
  // The transient stage preview: the model whose load we're previewing (projected onto the
  // bar) and the set of resident models the server says it would evict.
  const stagedModel = stagedId !== null ? (models.find((m) => m.id === stagedId) ?? null) : null;
  const stagedProjected = stagedModel !== null && !stagedModel.loaded ? stagedModel : null;
  const victimIds = new Set((plan?.victims ?? []).map((v) => v.id));
  // Catalog models not on the box: the install rows. Queued ones carry an in-flight
  // download the sync one-shot is pulling.
  const notEnabled = hostingEnabled ? models.filter((m) => !m.enabled) : [];
  const queued = notEnabled.filter((m) => m.queued);
  const queuedGb = queued.reduce((sum, m) => sum + m.size_gb, 0);
  // Provisioned models queued for removal — they apply through the same sync one-shot
  // as installs, so a pending uninstall surfaces the download bar too (otherwise a
  // queued removal would have no in-app control to apply it).
  const removing = hostingEnabled ? models.filter((m) => m.enabled && m.remove_queued) : [];
  // Resident footprint = weights + KV for everything actually loaded.
  const residentGb = loaded.reduce((sum, m) => sum + residentGbOf(m), 0);
  const stagedGb = stagedProjected !== null ? residentGbOf(stagedProjected) : 0;

  // The image service's live VRAM draw shares the meter. ComfyUI has no per-model
  // "loaded" flag, so a non-trivial VRAM draw stands in for "a model is resident".
  const imgMem = image?.memory ?? null;
  const imgUsedGb = imgMem ? Math.max(imgMem.total_gb - imgMem.free_gb, 0) : 0;
  const imgActive = (image?.reachable ?? false) && imgUsedGb > IMG_ACTIVE_GB;
  const imgName =
    (image?.models?.find((m) => m.enabled) ?? image?.models?.[0])?.label.split(" ")[0] ?? "Image";

  // Real system memory pressure the residency floor is enforced against (/proc/meminfo used,
  // reclaimable cache excluded) — what the evictor actually sees. The bar MUST show it: the
  // per-model segments alone omit the OS + the other on-box containers (postgres/api/worker/
  // embed…), so without this the bar looks half-empty while a load still evicts.
  const usedGb = hostMemory?.used_gb ?? null;
  // The used memory the per-model + image segments don't account for: OS + other containers,
  // plus any drift between catalog footprints and real RSS. Drawn as its own segment so the
  // bar fills to the SAME `used` the floor budgets against — a staged eviction then lines up
  // with the bar instead of looking like free room. Clamped at 0 (estimates can run over RSS).
  const systemGb = usedGb !== null ? Math.max(usedGb - residentGb - imgUsedGb, 0) : 0;
  // Honest "used" for the caption: measured used when we have it, else what we can see.
  const shownUsedGb = usedGb ?? residentGb + imgUsedGb;

  const anyOn = hostingEnabled || (image?.reachable ?? false);
  const summary = [
    hostingEnabled
      ? `${loaded.length} resident${stagedProjected !== null ? " · previewing" : ""}`
      : "",
    image?.reachable ? (imgActive ? "image resident" : "image idle") : "",
    anyOn ? `${Math.round(shownUsedGb)} GB` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  // Loaded segments first (resident), then the staged (projected) preview — colored by slot.
  const onBar = stagedProjected !== null ? [...loaded, stagedProjected] : loaded;
  // The bar is ALWAYS scaled to physical total (100% = the box), so the free-RAM floor
  // marker sits at a fixed ceiling/total regardless of the preview. When a preview
  // over-subscribes (resident + staged > total) the incoming segment simply runs off the
  // right edge (clipped) — an honest "it won't fit", reinforced by the "⚠ over" caption.
  const total = hostMemory?.total_gb ?? imgMem?.total_gb ?? 0;
  // Projected footprint after the load: the server's dry-run when measured, else a local
  // estimate (resident + staged − evicted).
  const victimGb = (plan?.victims ?? []).reduce((sum, v) => sum + v.gb, 0);
  const projectedGb =
    plan?.measured === true ? plan.projected_gb : residentGb + stagedGb - victimGb;
  const over = plan?.over ?? false;
  const meterShown = total > 0 && (onBar.length > 0 || imgActive);

  const llmMeta = hostingEnabled
    ? `${available.length} available · ${loaded.length} resident`
    : "off";
  const imgMeta = image?.reachable
    ? imgActive
      ? "running · resident"
      : "running"
    : image?.enabled
      ? "stopped"
      : "off";

  // LLM tab filtering (reversed order): resident (loaded) · available (roster) · catalogue (all).
  const llmRows = llmTab === "resident" ? loaded : llmTab === "available" ? available : models;

  return (
    <section className="onbox-card" aria-label={`On-box models — ${anyOn ? summary : "off"}`}>
      {/* Shared, always-visible unified-memory meter (LLM + image). */}
      <div className="onbox-head">
        <div className="onbox-status">
          <span
            className={`llm-local-dot${loaded.length > 0 || imgActive ? " on" : stagedProjected !== null ? " amber" : ""}`}
            aria-hidden="true"
          />
          <span className="onbox-status-title">On-box memory</span>
          <span className="onbox-status-sub">{anyOn ? summary : "off"}</span>
        </div>
        {meterShown ? (
          <div className="llm-mem" aria-label="unified memory in use">
            <div className="llm-mem-bar">
              {/* The OS + non-LLM containers the evictor counts but the model segments don't —
                  drawn first so the bar's fill matches the real `used` the floor budgets on. */}
              {systemGb > 0.5 && (
                <div
                  className="llm-mem-seg llm-mem-sys"
                  style={{ width: `${(systemGb / total) * 100}%` }}
                  title={`System + other containers — ${Math.round(systemGb)} GB (OS, database, on-box services)`}
                >
                  <div className="llm-mem-w" style={{ width: "100%" }} />
                  <span className="llm-mem-label">
                    system <span className="gb">{Math.round(systemGb)}G</span>
                  </span>
                </div>
              )}
              {onBar.map((m, i) => {
                const weights = m.disk_gb ?? m.size_gb;
                const res = weights + m.kv_gb;
                const isStaged = stagedProjected !== null && m.id === stagedProjected.id;
                const isVictim = victimIds.has(m.id);
                return (
                  <div
                    key={m.id}
                    className={`llm-mem-seg${isStaged ? " staged" : ""}${isVictim ? " evicting" : ""}`}
                    style={{ width: `${(res / total) * 100}%` }}
                    title={`${m.label} — ${weights} GB weights + ${m.kv_gb} GB KV${
                      isStaged ? " (staged)" : isVictim ? " (would be evicted)" : ""
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
              {imgActive && (
                <div
                  className="llm-mem-seg llm-mem-img"
                  style={{ width: `${(imgUsedGb / total) * 100}%` }}
                  title={`ComfyUI image — ${Math.round(imgUsedGb)} GB resident`}
                >
                  <div className="llm-mem-w" style={{ width: "100%", background: IMG_GRADIENT }} />
                  <span className="llm-mem-label">
                    {imgName} <span className="gb">{Math.round(imgUsedGb)}G</span>
                  </span>
                </div>
              )}
              {/* The keep-free floor marker — only while previewing a measured load, when it
                  explains the eviction. */}
              {stagedProjected !== null && plan?.measured === true && (
                <div
                  className="llm-mem-floor"
                  style={{ left: `${(plan.ceiling_gb / total) * 100}%` }}
                  aria-hidden="true"
                />
              )}
            </div>
            <div className="llm-mem-cap">
              <span>{Math.round(shownUsedGb)} GB used</span>
              {systemGb > 0.5 && (
                <span className="onbox-mem-key">
                  <span className="onbox-mem-sw sys" />
                  system {Math.round(systemGb)} GB
                </span>
              )}
              {imgActive && (
                <span className="onbox-mem-key">
                  <span className="onbox-mem-sw" />
                  image {Math.round(imgUsedGb)} GB
                </span>
              )}
              {stagedProjected !== null &&
                (victimIds.size > 0 ? (
                  <span className="staged-note over">
                    evicts {(plan?.victims ?? []).map((v) => v.label.split(" ")[0]).join(", ")} →{" "}
                    {Math.round(projectedGb)} GB{over ? " ⚠ still over" : ""}
                  </span>
                ) : (
                  <span className="staged-note">
                    +{Math.round(stagedGb)} GB staged → {Math.round(projectedGb)} GB
                  </span>
                ))}
              <span className="total">{Math.round(total)} GB total</span>
            </div>
          </div>
        ) : (
          <p className="onbox-mem-empty">
            Nothing resident — load a model, or run image generation, to fill the bar.
          </p>
        )}
        {hostingEnabled && (
          <>
            <FreeRamControl freeRam={freeRam} busy={freeRamBusy} onChange={onSetFreeRam} />
            <AutoRestoreControl
              enabled={autoRestore}
              busy={autoRestoreBusy}
              onChange={onSetAutoRestore}
            />
          </>
        )}
      </div>

      {/* Section 1: On-box LLMs */}
      <SectionToggle
        accent="llm"
        name="On-box LLMs"
        meta={llmMeta}
        open={llmOpen}
        onToggle={onToggleLlm}
      />
      {llmOpen && (
        <div className="onbox-body onbox-llm open">
          <OmniTabs
            label="On-box LLM models"
            active={llmTab}
            onTab={onLlmTab}
            tabs={[
              { id: "resident", label: "Resident", icon: "resident" },
              { id: "available", label: "Available", icon: "available" },
              { id: "catalogue", label: "Catalogue", icon: "catalog" },
            ]}
          />
          <div className="onbox-list">
            {!hostingEnabled && (
              <p className="llm-local-hint">
                Self-hosting is off. Provision on the server with{" "}
                <code>jbrain enable-local-models</code>; models you enable there become selectable
                in the tiers above.
              </p>
            )}
            {hostingEnabled && llmTab === "resident" && loaded.length === 0 && (
              <p className="onbox-tab-hint">
                Nothing resident. Stage an available model to load one — staging previews whether it
                would evict anything first.
              </p>
            )}
            {hostingEnabled && llmTab === "available" && (
              <p className="onbox-tab-hint">
                {stagedProjected !== null
                  ? "Previewing a stage — Load to commit, or Cancel. The memory bar shows what it evicts."
                  : "Installed models the router may swap in. Stage previews whether loading one evicts something."}
              </p>
            )}
            {hostingEnabled && llmTab === "available" && available.length === 0 && (
              <p className="llm-local-hint">
                No models available yet — install one, or switch one on, from the Catalogue tab.
              </p>
            )}
            {hostingEnabled && llmTab === "catalogue" && (
              <p className="onbox-tab-hint">
                Every catalogue model. Install pulls the weights; make an installed model available
                to let the router swap it in.
              </p>
            )}
            {hostingEnabled &&
              llmRows.map((m) =>
                llmTab === "catalogue" ? (
                  m.enabled ? (
                    <UninstallRow
                      key={m.id}
                      model={m}
                      busy={busy.has(m.id)}
                      onUninstall={onUninstall}
                      onSetAvailable={onSetAvailable}
                    />
                  ) : (
                    <InstallRow
                      key={m.id}
                      model={m}
                      busy={busy.has(m.id)}
                      onInstall={onInstall}
                      onUninstall={onUninstall}
                    />
                  )
                ) : (
                  <LlmModelRow
                    key={m.id}
                    model={m}
                    busy={busy.has(m.id)}
                    staged={stagedId === m.id}
                    isVictim={victimIds.has(m.id)}
                    previewing={stagedId !== null}
                    plan={stagedId === m.id ? plan : null}
                    onUnload={onUnload}
                    onStage={onStage}
                    onLoad={onLoad}
                    onCancelStage={onCancelStage}
                    onSetWindow={onSetWindow}
                    onSetSlots={onSetSlots}
                    onSetImageFloor={onSetImageFloor}
                  />
                ),
              )}
          </div>
          {llmTab !== "resident" &&
            (queued.length > 0 || removing.length > 0 || downloadState !== "idle") && (
              <div className="llm-local-queue">
                <div className="llm-local-queue-text">
                  <b>
                    {[
                      queued.length > 0 &&
                        `${queued.length} to download · ${Math.round(queuedGb)} GB`,
                      removing.length > 0 && `${removing.length} to remove`,
                    ]
                      .filter(Boolean)
                      .join(" · ") || "Local models"}
                  </b>
                  <span>
                    {downloadState === "running"
                      ? downloadTail || "Downloading weights…"
                      : downloadState === "failed"
                        ? `Download failed — ${downloadTail || "check the Ops screen"}`
                        : "Starts on install; tap Download to retry."}
                  </span>
                </div>
                <button
                  type="button"
                  className="llm-local-btn load"
                  disabled={downloadState === "running"}
                  onClick={onDownload}
                >
                  {downloadState === "running"
                    ? "Working…"
                    : queued.length > 0
                      ? "Download now"
                      : "Apply now"}
                </button>
              </div>
            )}
        </div>
      )}

      {/* Section 2: Image models */}
      <SectionToggle
        accent="img"
        name="Image models"
        meta={imgMeta}
        open={imgOpen}
        onToggle={onToggleImg}
      />
      {imgOpen && image && (
        <div className="onbox-body onbox-img open">
          <ImageServiceRow
            image={image}
            onFree={onFreeImage}
            onStart={onStartImageService}
            onStop={onStopImageService}
          />
          <OmniTabs
            label="Image models"
            active={imgTab}
            onTab={onImgTab}
            tabs={[
              { id: "installed", label: "Installed", icon: "installed" },
              { id: "catalog", label: "Catalog", icon: "catalog" },
            ]}
          />
          <div className="onbox-list">
            {imgTab === "catalog" && (
              <p className="onbox-tab-hint">
                Image catalog. Provision the weights on the box with <code>comfyui-setup.sh</code>.
                One model is resident at a time.
              </p>
            )}
            {(image.models ?? [])
              .filter((m) => imgTab === "catalog" || m.enabled)
              .map((m) => imageRow(m))}
            {!image.enabled && (
              <p className="llm-local-hint">
                Image generation is off. Provision on the box with <code>comfyui-setup.sh</code>.
              </p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

// One provisioned LLM in the Resident/Available tabs. Resident → Unload; an available model
// → Stage (previews the load's eviction) or, if it's the one being previewed, shows "staged".
// A row the current preview would evict is flagged "will evict". Plus capability chips and the
// live context-window picker (locked while resident).
function LlmModelRow({
  model: m,
  busy: isBusy,
  staged,
  isVictim,
  previewing,
  plan,
  onUnload,
  onStage,
  onLoad,
  onCancelStage,
  onSetWindow,
  onSetSlots,
  onSetImageFloor,
}: {
  model: LocalModelInfo;
  busy: boolean;
  // This row is the model currently being previewed (transient stage).
  staged: boolean;
  // This resident model would be evicted by the current preview.
  isVictim: boolean;
  // A stage preview is open (for some model) — disable Stage on other rows meanwhile.
  previewing: boolean;
  // The dry-run plan for THIS row when it's the staged one (null otherwise).
  plan: LoadPlan | null;
  onUnload: (id: string) => void;
  onStage: (id: string) => void;
  onLoad: (id: string) => void;
  onCancelStage: () => void;
  onSetWindow: (id: string, window: number | null) => void;
  onSetSlots: (id: string, slots: number | null) => void;
  onSetImageFloor: (id: string, tokens: number | null) => void;
}) {
  const footprint = m.disk_gb ?? m.size_gb;
  const sizeText = `${m.disk_gb == null ? "~" : ""}${footprint} GB`;
  const stateText = isVictim
    ? "will evict"
    : m.loaded
      ? "resident"
      : staged
        ? "staged"
        : "available";
  const stateCls = isVictim ? " evicting" : m.loaded ? " on" : staged ? " staged" : " avail";
  // When this row is staged, the load's consequence (from the server dry-run) shown inline
  // beside its Load / Cancel — a too-big model can't be loaded (the server would 409).
  const tooBig = staged && plan?.over_box === true;
  const stagedNote = !staged
    ? null
    : tooBig
      ? `Too big for this box — needs ~${Math.round(plan?.projected_gb ?? 0)} GB but only ${Math.round(
          plan?.total_gb ?? 0,
        )} GB exists. Loading it would crash the box.`
      : plan?.measured === false
        ? "Ready to load — couldn't measure the box for an eviction preview."
        : (plan?.victims.length ?? 0) > 0
          ? `Evicts ${(plan?.victims ?? [])
              .map((v) => v.label)
              .join(", ")} → ${Math.round(plan?.projected_gb ?? 0)} GB resident.`
          : `Fits — no eviction. → ${Math.round(plan?.projected_gb ?? 0)} GB resident.`;
  const editable = !m.loaded; // available (not resident) — no live process to disrupt
  const effWindow = m.context_window_override ?? m.context_window;
  // Choices run up to the model's native ceiling (not the conservative served
  // default), so the operator can opt into a bigger window the weights support.
  // Always keep the served default and the current value selectable.
  const windowOpts = Array.from(
    new Set([
      ...WINDOW_CHOICES.filter((w) => w <= m.max_context_window),
      m.context_window,
      m.max_context_window,
      effWindow,
    ]),
  ).sort((a, b) => a - b);
  return (
    <div className={`llm-local-row on${isVictim ? " evicting" : staged ? " staged" : ""}`}>
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
            {m.loaded ? (
              <button
                type="button"
                className="llm-local-btn"
                disabled={isBusy}
                onClick={() => onUnload(m.id)}
              >
                {isBusy ? "…" : "Unload"}
              </button>
            ) : staged ? (
              // The previewed row drives Load / Cancel inline, right where Stage was.
              <>
                <button
                  type="button"
                  className="llm-local-btn load"
                  disabled={isBusy || tooBig}
                  onClick={() => onLoad(m.id)}
                >
                  {isBusy ? "…" : tooBig ? "Can't load" : "Load now"}
                </button>
                <button type="button" className="llm-local-btn" onClick={onCancelStage}>
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="llm-local-btn stage"
                disabled={isBusy || previewing}
                onClick={() => onStage(m.id)}
              >
                {isBusy ? "…" : "Stage"}
              </button>
            )}
          </div>
          <span className={`llm-local-state${stateCls}`}>{stateText}</span>
        </div>
      </div>
      {stagedNote && <p className="llm-local-note">{stagedNote}</p>}
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
      <div className="llm-local-ctx">
        <label className="llm-local-ctx-label" htmlFor={`slots-${m.id}`}>
          interactive slot
        </label>
        <select
          id={`slots-${m.id}`}
          className="llm-local-ctx-select"
          value={String(m.parallel_slots)}
          disabled={!editable || isBusy}
          title="A second slot keeps this model's chat prefix warm in its own KV cache, so background jobs and chat-titling can't evict it — the first message after a restart stays instant. Doubles the model's KV cost."
          onChange={(e) => {
            const v = Number(e.target.value);
            // 1 is the default (single slot) — store null so no redundant override row persists.
            onSetSlots(m.id, v <= 1 ? null : v);
          }}
        >
          <option value="1">off (shared slot)</option>
          <option value="2">on (dedicated · 2× KV)</option>
        </select>
        {!m.loaded && <span className="llm-local-ctx-meta">keeps chat instant after restart</span>}
      </div>
      {/* Vision entries only. A floor on a text-only model would never be read, so the row is
          absent rather than present-and-inert — the drawer should not offer a dead control. */}
      {m.image_min_tokens !== null && (
        <div className="llm-local-ctx">
          <label className="llm-local-ctx-label" htmlFor={`imgmin-${m.id}`}>
            image detail
          </label>
          <select
            id={`imgmin-${m.id}`}
            className="llm-local-ctx-select"
            value={String(m.image_min_tokens)}
            disabled={!editable || isBusy}
            title="The floor an image is encoded to. Raise it when small text comes back garbled — a curved bottle label, a receipt, a screenshot of a table. Costs prefill time and KV, not weights, so it does not change how much memory the model needs to load."
            onChange={(e) => {
              const v = Number(e.target.value);
              // Storing the catalog's own floor as an override would persist a redundant row,
              // so match the context-window control and send null for it.
              onSetImageFloor(m.id, v === m.image_min_tokens_default ? null : v);
            }}
          >
            {IMAGE_FLOOR_OPTS.map((t) => (
              <option key={t} value={t}>
                {fmtTokens(t)}
                {t === m.image_min_tokens_default ? " (default)" : ""}
              </option>
            ))}
          </select>
          {m.loaded ? (
            <span className="llm-local-ctx-hint">🔒 unload to change</span>
          ) : (
            <span className="llm-local-ctx-meta">higher = small text stays legible</span>
          )}
        </div>
      )}
    </div>
  );
}

// llama.cpp caps this projector family at 4096 (`set_limit_image_tokens(8, 4096)`), so the
// list stops there — offering more would promise detail the engine will not deliver.
const IMAGE_FLOOR_OPTS = [256, 512, 1024, 2048, 4096];

// A destructive action button that requires a second tap to confirm — replaces a
// browser confirm() dialog for the weight-deleting Uninstall/Remove. First tap arms it
// (the label flips to "Confirm?"); a second tap within a few seconds fires onConfirm,
// otherwise it disarms itself. Local state, so each row's button arms independently.
function ConfirmButton({
  label,
  busy,
  onConfirm,
}: {
  label: string;
  busy: boolean;
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  function onClick() {
    if (timer.current) clearTimeout(timer.current);
    if (armed) {
      setArmed(false);
      onConfirm();
    } else {
      setArmed(true);
      timer.current = setTimeout(() => setArmed(false), 3000);
    }
  }
  return (
    <button
      type="button"
      className={`llm-local-btn danger${armed ? " armed" : ""}`}
      disabled={busy}
      aria-label={armed ? `Confirm ${label.toLowerCase()}` : label}
      onClick={onClick}
    >
      {busy ? "…" : armed ? "Confirm?" : label}
    </button>
  );
}

// A provisioned model in the Catalogue tab: an Available switch (make it routable / take it
// out of the roster without deleting weights), its state chip, and a tap-to-confirm
// "Uninstall" (queues the removal + kicks the sync one-shot; "Keep" backs out while queued).
function UninstallRow({
  model: m,
  busy,
  onUninstall,
  onSetAvailable,
}: {
  model: LocalModelInfo;
  busy: boolean;
  onUninstall: (id: string, on: boolean) => void;
  onSetAvailable: (id: string, on: boolean) => void;
}) {
  const footprint = m.disk_gb ?? m.size_gb;
  const sizeText = `${m.disk_gb == null ? "~" : ""}${footprint} GB`;
  const state = m.loaded ? "resident" : m.available ? "available" : "unavailable";
  const stateCls = m.remove_queued ? " removing" : m.available || m.loaded ? " avail" : "";
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
              <>
                <button
                  type="button"
                  className={`llm-local-btn${m.available ? "" : " stage"}`}
                  disabled={busy}
                  onClick={() => onSetAvailable(m.id, !m.available)}
                >
                  {busy ? "…" : m.available ? "Make unavailable" : "Make available"}
                </button>
                <ConfirmButton
                  label="Uninstall"
                  busy={busy}
                  onConfirm={() => onUninstall(m.id, true)}
                />
              </>
            )}
          </div>
          <span className={`llm-local-state${stateCls}`}>
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

// A catalog model that is NOT in the enabled roster. Two shapes:
//   • not on disk → a plain Install (queues the download; a live progress bar follows
//     download_gb / size_gb once the sync pulls it, so the weight pull needs no shell);
//   • on disk but disabled (dropped from LOCAL_MODELS — an orphaned alt) → Enable
//     re-adds it to the roster with NO re-download, and Remove reclaims its weights.
// Enable rides the same install queue as a download (instant when the weights exist);
// Remove rides the uninstall queue + the sync's guarded prune.
function InstallRow({
  model,
  busy,
  onInstall,
  onUninstall,
}: {
  model: LocalModelInfo;
  busy: boolean;
  onInstall: (id: string, on: boolean) => void;
  onUninstall: (id: string, on: boolean) => void;
}) {
  const onDisk = model.disk_gb != null;
  const downloading = model.queued && model.download_gb != null;
  const pct = downloading
    ? Math.min(100, Math.round(((model.download_gb ?? 0) / model.size_gb) * 100))
    : 0;
  const sizeText = onDisk ? `${model.disk_gb} GB on disk` : `~${model.size_gb} GB`;
  return (
    <div
      className={`llm-local-row install${model.queued ? " queued" : ""}${
        model.remove_queued ? " removing" : ""
      }`}
    >
      <div className="llm-local-head">
        <div className="llm-local-name">
          {model.label}
          <span className="llm-local-meta">
            {model.quant} · {sizeText}
          </span>
        </div>
        <div className="llm-local-topright">
          <div className="llm-local-act">
            {model.remove_queued ? (
              // Removal queued/applying — offer only "Keep" to back out before the prune.
              <button
                type="button"
                className="llm-local-btn"
                disabled={busy}
                onClick={() => onUninstall(model.id, false)}
              >
                {busy ? "…" : "Keep"}
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className={`llm-local-btn${model.queued ? "" : " load"}`}
                  disabled={busy}
                  onClick={() => onInstall(model.id, !model.queued)}
                >
                  {busy ? "…" : model.queued ? "Cancel" : onDisk ? "Enable" : "Install"}
                </button>
                {/* On disk but disabled: reclaim its weights without enabling first. */}
                {onDisk && !model.queued && (
                  <ConfirmButton
                    label="Remove"
                    busy={busy}
                    onConfirm={() => onUninstall(model.id, true)}
                  />
                )}
              </>
            )}
          </div>
          {model.remove_queued ? (
            <span className="llm-local-state removing">uninstalling</span>
          ) : model.queued ? (
            <span className="llm-local-state queued">queued</span>
          ) : null}
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

// The image section's service control row: ComfyUI's reachability status plus its
// Start / Stop / Free controls. The catalog rows render below it in the card's tab
// body; provisioning (the weight download) stays the on-box comfyui-setup.sh step.
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

function imageRow(m: ImageModelInfo) {
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
