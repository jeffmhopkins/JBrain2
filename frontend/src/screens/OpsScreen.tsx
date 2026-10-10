import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type ContainerStatus,
  type HostSettings,
  type MetricRange,
  type MetricsHistory,
  type MinecraftStatus,
  type MinecraftVersion,
  type OpsMetrics,
  type PanelStatusOut,
  type UpdateStatus,
  api,
} from "../api/client";
import { Dialog } from "../components/Dialog";
import { LocalEngineSection, engineGlance } from "../components/LocalEngineCard";
import { OpsCard } from "../components/OpsCard";
import { PageLayer } from "../components/PageLayer";
import { TimeSeriesPlot } from "../components/TimeSeriesPlot";
import {
  BotIcon,
  ClockIcon,
  CubeIcon,
  DatabaseIcon,
  LayersIcon,
  MemoryIcon,
  MonitorIcon,
  ShieldIcon,
} from "../components/icons";
import { serverMetricSeries } from "../components/serverMetricSeries";
import { clearEngineFocus, useEngineSnapshot } from "../engineState";
import {
  type ConfirmSpec,
  MC_SERVICE,
  type McLevel,
  confirmContext,
  confirmFor,
  glanceOf,
  tileOf,
} from "../minecraft";
import { agoLabel, panelConcerns, panelFacts, panelHealth } from "../panelStatus";
import { useForeground, useForegroundRef } from "../visibility";
import { RunsScreen } from "./RunsScreen";

function fmtBytes(n: number): string {
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(1)} GB`;
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(0)} MB`;
  return `${(n / 1024).toFixed(0)} KB`;
}

/** Split system memory the way `free`/`htop` do: reclaimable page cache is
 * AVAILABLE, not used. `used` is only the non-reclaimable occupancy (process RSS +
 * iGPU device memory + kernel/slab); `cache` is reclaimable (freed the instant
 * something needs it); `free` is untouched. So a box that has merely cached model
 * files from disk reads as nearly empty, not ~13% "used". Falls back to the
 * kernel's MemAvailable when the supervisor doesn't report the breakdown. */
function memParts(m: OpsMetrics): {
  total: number;
  used: number;
  cache: number;
  free: number;
} {
  const total = m.mem_total_bytes;
  const mb = m.mem_breakdown;
  if (!mb) {
    const used = Math.max(0, total - m.mem_available_bytes);
    return { total, used, cache: 0, free: Math.max(0, m.mem_available_bytes) };
  }
  const cache = (mb.Cached ?? 0) + (mb.Buffers ?? 0);
  const free = mb.MemFree ?? Math.max(0, total - m.mem_available_bytes);
  const used = Math.max(0, total - free - cache);
  return { total, used, cache, free };
}

function fmtUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  return d > 0 ? `${d}d ${h}h` : `${h}h ${Math.floor((seconds % 3600) / 60)}m`;
}

// The gradient is anchored to the full track (an opaque overlay masks the unused
// right portion), so a bar's color reflects ABSOLUTE load — green low, red only
// near full — matching the LLM memory meter's look. `util` drops the red stop:
// a pegged GPU during inference is healthy, not alarming.
function Meter({
  used,
  total,
  tone = "resource",
}: {
  used: number;
  total: number;
  tone?: "resource" | "util";
}) {
  const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
  return (
    <div className={`meter meter-${tone}`}>
      <div className="meter-empty" style={{ width: `${100 - pct}%` }} />
    </div>
  );
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "Request failed. Is the server reachable?";
}

function badgeClass(value: string): string {
  if (value === "running" || value === "healthy") return "badge ok";
  if (value === "exited" || value === "dead" || value === "unhealthy") return "badge bad";
  return "badge warn";
}

// ===== Health levels — the roll-up that colors service dots and group state =====

// "off" is a service stopped on purpose. It is drawn grey and never counts against its group,
// so a box with its opt-in extras switched off reads as healthy — before it did, AI and
// "AI - Optional" sat amber and red for good and the colour stopped meaning anything.
type Level = "ok" | "warn" | "bad" | "off";
const LEVEL_RANK: Record<Level, number> = { off: 0, ok: 0, warn: 1, bad: 2 };

// Services the box runs only when switched on (a compose profile): stopped is their normal
// resting state. The chosen engine is the exception — it is opt-in to the stack but the one
// local AI runs on, so it stopping is a failure, not a choice (see `svcLevel`).
const OPT_IN_SERVICES = new Set([
  "local-llm",
  "flash-next",
  "comfyui",
  "jcode",
  "sdr",
  "mqtt",
  "mqtt-ingest",
  "cloudflared",
  "migrate",
  "wipe",
]);

/** `chosenEngine` is the compose service of the engine the owner chose, when known. */
function svcLevel(c: ContainerStatus, chosenEngine: string | null): Level {
  const stopped = c.state === "exited" || c.state === "created";
  if (stopped && OPT_IN_SERVICES.has(c.service) && c.service !== chosenEngine) return "off";
  if (c.state === "exited" || c.state === "dead") return "bad";
  if (c.health === "unhealthy") return "bad";
  if (c.health === "starting" || c.state === "restarting" || c.state === "created") return "warn";
  if (c.state === "running") return c.health === null || c.health === "healthy" ? "ok" : "warn";
  return "warn";
}

function worse(a: Level, b: Level): Level {
  return LEVEL_RANK[b] > LEVEL_RANK[a] ? b : a;
}

function groupLevel(items: ContainerStatus[], chosenEngine: string | null): Level {
  return items.reduce<Level>((w, c) => worse(w, svcLevel(c, chosenEngine)), "ok");
}

/** Services are grouped by what they are FOR, so the list stays scannable as the stack grows.
 * Grouping is frontend-only — the backend status payload is flat. Every compose service is
 * assigned a group here; anything unrecognized still falls into a trailing "Other" group, and
 * empty groups don't render. */
const SERVICE_GROUPS: { label: string; services: string[] }[] = [
  // The app itself and the way in: without any one of these the app is down or unreachable
  // (db/web/postgres are alias names some deploys use).
  {
    label: "Core",
    services: ["api", "worker", "supervisor", "db", "postgres", "web", "proxy", "cloudflared"],
  },
  // Everything that runs a model on the box, opt-in ones included — they show as off.
  {
    label: "Models",
    services: ["flash-next", "local-llm", "embed", "tts-stt", "rapidocr", "comfyui"],
  },
  // What the assistant reaches for mid-answer.
  {
    label: "Assistant tools",
    services: [
      "searxng",
      "reader",
      "byparr",
      "browser",
      "egress",
      "htmlrender",
      "pysandbox",
      "jcode",
    ],
  },
  // Hardware and screens around the house, and the phones' location feed.
  { label: "Devices", services: ["wall", "endpoint", "sdr", "mqtt", "mqtt-ingest"] },
  { label: "Apps", services: ["minecraft", "jlaunch"] },
  // Run-once maintenance containers — present only while one runs.
  { label: "One-shot jobs", services: ["migrate", "wipe"] },
];

// What each service is, in the owner's words, beside its compose name.
const SERVICE_WHAT: Record<string, string> = {
  api: "app server",
  worker: "background jobs",
  supervisor: "updates and restarts",
  db: "database",
  postgres: "database",
  proxy: "front door",
  cloudflared: "remote-access tunnel",
  "flash-next": "chat engine",
  "local-llm": "Standard engine",
  embed: "search embeddings",
  "tts-stt": "speech in and out",
  rapidocr: "text from images",
  comfyui: "image generation",
  searxng: "web search",
  reader: "page reader",
  byparr: "bot-challenge solver",
  browser: "browse agent's browser",
  egress: "browser's filtered way out",
  htmlrender: "HTML to image",
  pysandbox: "runs Python",
  jcode: "code mode",
  wall: "wall display",
  endpoint: "panel flasher",
  sdr: "radio",
  mqtt: "location broker",
  "mqtt-ingest": "location feed",
  minecraft: "Bedrock server",
  jlaunch: "long compute jobs",
  migrate: "schema migration",
  wipe: "install reset",
};

function groupContainers(
  containers: ContainerStatus[],
): { label: string; items: ContainerStatus[] }[] {
  const groups = SERVICE_GROUPS.map((g) => ({ label: g.label, items: [] as ContainerStatus[] }));
  const other: ContainerStatus[] = [];
  for (const c of containers) {
    const group = groups.find((_, i) => SERVICE_GROUPS[i]?.services.includes(c.service));
    if (group) group.items.push(c);
    else other.push(c);
  }
  const result = groups.filter((g) => g.items.length > 0);
  if (other.length > 0) result.push({ label: "Other", items: other });
  return result;
}

// ===== Server update — the one Update on the screen, right under the vitals =====

type UpdatePhase =
  | { step: "idle" }
  | { step: "confirm" }
  | { step: "running"; log: string; unreachable: boolean }
  | { step: "done"; ok: boolean; log: string };

const UPDATE_POLL_MS = 3000;

// Disk allowances the owner picks from; a stored value outside the list is shown as-is.
const PROMPT_CACHE_BUDGETS_GB = [10, 25, 40, 60, 80, 120, 200];

type RestoreGate = "awaiting_probe" | "passed" | "failed";

// What the restore gate means for the owner: saves always happen, restores wait for the probe.
const GATE_HINT: Record<RestoreGate, string> = {
  awaiting_probe: " Restores wait for the engine's slot check.",
  passed: "",
  failed: " Restores are off: the engine's slot check failed.",
};

/** The prompt cache's two owner knobs (FLASH_NEXT_ENGINE_PLAN F4), on the Engine page: whether
 *  Flash-Next keeps each chat conversation on disk across slot changes, restarts and engine
 *  switches, and how much disk the cache may use. Both apply at once — no Update needed. Each
 *  write is optimistic and put back on a refusal: a control that lies about what the box will
 *  do is worse than one that lags. */
function PromptCacheControls() {
  const [conversations, setConversations] = useState<boolean | null>(null);
  const [budget, setBudget] = useState<number | null>(null);
  const [gate, setGate] = useState<RestoreGate | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const settings = await api.getSettings();
        if (cancelled) return;
        if (typeof settings.llm_kv_conversation_cache === "boolean") {
          setConversations(settings.llm_kv_conversation_cache);
        }
        if (typeof settings.llm_kv_prefix_budget_gb === "number") {
          setBudget(settings.llm_kv_prefix_budget_gb);
        }
        setGate(settings.llm_kv_restore_gate ?? null);
      } catch {
        // Leave both unknown rather than guessing a state the owner might act on.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function toggle(): Promise<void> {
    if (conversations === null) return;
    const next = !conversations;
    setConversations(next);
    try {
      await api.updateSettings({ llm_kv_conversation_cache: next });
    } catch {
      setConversations(!next);
    }
  }

  async function pickBudget(next: number): Promise<void> {
    if (budget === null || next === budget) return;
    const previous = budget;
    setBudget(next);
    try {
      await api.updateSettings({ llm_kv_prefix_budget_gb: next });
    } catch {
      setBudget(previous);
    }
  }

  const options =
    budget !== null && !PROMPT_CACHE_BUDGETS_GB.includes(budget)
      ? [...PROMPT_CACHE_BUDGETS_GB, budget].sort((a, b) => a - b)
      : PROMPT_CACHE_BUDGETS_GB;

  return (
    <>
      <div className="settings-switch-row ops-autoupdate">
        <span className="settings-meta" style={{ margin: 0 }}>
          Keep chats on disk{" "}
          <span className="muted">
            — research chats only; Brain chats never leave the database.
            {gate !== null && GATE_HINT[gate]}
          </span>
        </span>
        <button
          type="button"
          role="switch"
          aria-label="Keep chats on disk (Flash-Next conversation cache)"
          aria-checked={conversations ?? false}
          className={`settings-switch${conversations ? " on" : ""}`}
          disabled={conversations === null}
          onClick={() => void toggle()}
        >
          <span className="knob" />
        </button>
      </div>
      <div className="settings-switch-row ops-autoupdate">
        <span className="settings-meta" style={{ margin: 0 }}>
          Prompt cache disk{" "}
          <span className="muted">
            — saved prompts and chats, per engine; the oldest chats go first.
            {gate !== null && GATE_HINT[gate]}
          </span>
        </span>
        <select
          aria-label="Prompt cache disk budget"
          value={budget ?? ""}
          disabled={budget === null}
          onChange={(e) => void pickBudget(Number(e.target.value))}
        >
          {budget === null && <option value="">…</option>}
          {options.map((gb) => (
            <option key={gb} value={gb}>
              {gb} GB
            </option>
          ))}
        </select>
      </div>
    </>
  );
}

function UpdateControl() {
  const [phase, setPhase] = useState<UpdatePhase>({ step: "idle" });
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  // While the app is backgrounded the status poll goes silent; the server-side
  // update runs on regardless, and the next foreground tick picks it back up.
  const foregroundRef = useForegroundRef();

  const stopPolling = useCallback(() => {
    if (timer.current !== null) clearInterval(timer.current);
    timer.current = null;
  }, []);
  useEffect(() => stopPolling, [stopPolling]);

  const poll = useCallback(async () => {
    if (!foregroundRef.current) return;
    let status: UpdateStatus;
    try {
      status = await api.opsUpdateStatus();
    } catch {
      // The stack restarts mid-update — the api going away briefly is
      // expected, not a failure. Keep polling.
      setPhase((p) => (p.step === "running" ? { ...p, unreachable: true } : p));
      return;
    }
    if (status.state === "running") {
      setPhase({ step: "running", log: status.log_tail, unreachable: false });
    } else if (status.state === "exited") {
      stopPolling();
      setPhase({ step: "done", ok: status.exit_code === 0, log: status.log_tail });
    }
  }, [stopPolling, foregroundRef]);

  async function start() {
    try {
      await api.opsUpdateStart();
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 409)) {
        setPhase({ step: "idle" });
        return;
      }
      // 409: an update is already running — just attach to it.
    }
    setPhase({ step: "running", log: "[update] starting", unreachable: false });
    timer.current = setInterval(() => void poll(), UPDATE_POLL_MS);
  }

  return (
    <div className="ops-update-row">
      {phase.step === "idle" && (
        <div className="ops-update-bar">
          <span className="ops-update-dot" />
          <span className="ops-update-text">
            <b>Server update</b> — latest on <code>main</code>
          </span>
          <button
            type="button"
            className="ops-update-btn"
            onClick={() => setPhase({ step: "confirm" })}
          >
            Update
          </button>
        </div>
      )}
      {phase.step === "confirm" && (
        <div className="ops-update-bar">
          <span className="ops-update-dot" />
          <span className="ops-update-text">pulls latest main, rebuilds, restarts</span>
          <button
            type="button"
            className="ops-update-btn danger"
            onClick={() => void start()}
            onBlur={() => setPhase({ step: "idle" })}
          >
            Tap again to update
          </button>
        </div>
      )}
      {phase.step === "running" && (
        <>
          <p className="muted">{phase.unreachable ? "Stack restarting — hold on…" : "Updating…"}</p>
          <pre className="ops-update-log">{phase.log}</pre>
        </>
      )}
      {phase.step === "done" && (
        <>
          <p className={phase.ok ? "muted" : "ops-error"}>
            {phase.ok ? "Update complete." : "Update failed — see log."}
          </p>
          <pre className="ops-update-log">{phase.log}</pre>
          {phase.ok && (
            <button type="button" onClick={() => window.location.reload()}>
              Reload app
            </button>
          )}
        </>
      )}
    </div>
  );
}

// ===== Vitals — the current readings, always open at the top =====

function VitalsCard({
  metrics,
  onRefresh,
  busy,
}: {
  metrics: OpsMetrics | null;
  onRefresh: () => void;
  busy: boolean;
}) {
  const refresh = (
    <button type="button" className="ops-refresh" onClick={onRefresh} disabled={busy}>
      {busy ? "Refreshing…" : "Refresh"}
    </button>
  );
  if (metrics === null) {
    return (
      <section className="ops-card ops-vitals" aria-label="Vitals">
        <p className="muted ops-vitals-empty">metrics unavailable.</p>
        <div className="ops-vitals-foot">{refresh}</div>
      </section>
    );
  }
  // Reclaimable cache counts as available, so the meter reflects real occupancy.
  const memUsed = memParts(metrics).used;
  const diskUsed = metrics.disk_total_bytes - metrics.disk_free_bytes;
  const pct = (used: number, total: number) => (total > 0 ? Math.round((used / total) * 100) : 0);
  return (
    <section className="ops-card ops-vitals" aria-label="Vitals">
      <div className="ops-vitals-grid">
        <div className="ops-vital">
          <span className="ops-vk">GPU</span>
          <span className="ops-vital-v">
            {metrics.gpu_busy_percent != null ? `${Math.round(metrics.gpu_busy_percent)}%` : "—"}
          </span>
          {metrics.gpu_busy_percent != null && (
            <Meter used={metrics.gpu_busy_percent} total={100} tone="util" />
          )}
        </div>
        <div className="ops-vital">
          <span className="ops-vk">Memory</span>
          <span className="ops-vital-v">
            {pct(memUsed, metrics.mem_total_bytes)}%{" "}
            <small>
              {fmtBytes(memUsed)} / {fmtBytes(metrics.mem_total_bytes)}
            </small>
          </span>
          <Meter used={memUsed} total={metrics.mem_total_bytes} />
        </div>
        <div className="ops-vital">
          <span className="ops-vk">Power</span>
          {/* APU/SoC package power (amdgpu), not wall power — a readout, no meter. */}
          <span className="ops-vital-v">
            {metrics.apu_power_w != null ? (
              <>
                {metrics.apu_power_w.toFixed(1)} <small>W APU</small>
              </>
            ) : (
              "—"
            )}
          </span>
        </div>
        <div className="ops-vital">
          <span className="ops-vk">Disk</span>
          <span className="ops-vital-v">
            {pct(diskUsed, metrics.disk_total_bytes)}%{" "}
            <small>
              {fmtBytes(diskUsed)} / {fmtBytes(metrics.disk_total_bytes)}
            </small>
          </span>
          <Meter used={diskUsed} total={metrics.disk_total_bytes} />
        </div>
      </div>
      <div className="ops-vitals-foot">
        <span>
          load {metrics.load_1m.toFixed(2)} · {metrics.load_5m.toFixed(2)} ·{" "}
          {metrics.load_15m.toFixed(2)} · up {fmtUptime(metrics.uptime_seconds)}
        </span>
        {refresh}
      </div>
    </section>
  );
}

// ===== Storage — the slower-moving numbers that left the vitals =====

function StorageRows({ metrics }: { metrics: OpsMetrics | null }) {
  if (metrics === null) return <p className="muted ops-vrow-empty">metrics unavailable.</p>;
  const diskUsed = metrics.disk_total_bytes - metrics.disk_free_bytes;
  const swapUsed = metrics.swap_total_bytes - metrics.swap_free_bytes;
  return (
    <>
      <div className="ops-vrow">
        <span className="ops-vk">Database</span>
        <div className="ops-vmid">
          {metrics.db ? (
            <>
              <span className="ops-vv">{fmtBytes(metrics.db.db_size_bytes)}</span>
              <span className="ops-vsub">
                {metrics.db.note_count} notes · {metrics.db.attachment_count} files
                {metrics.blobs ? ` · ${fmtBytes(metrics.blobs.total_bytes)} blobs` : ""}
              </span>
            </>
          ) : (
            <span className="ops-vsub">unavailable</span>
          )}
        </div>
      </div>
      <div className="ops-vrow">
        <span className="ops-vk">Disk</span>
        <div className="ops-vmid">
          <span className="ops-vv">
            {fmtBytes(diskUsed)} <small>/ {fmtBytes(metrics.disk_total_bytes)}</small>
          </span>
          <Meter used={diskUsed} total={metrics.disk_total_bytes} />
        </div>
      </div>
      {metrics.swap_total_bytes > 0 && (
        <div className="ops-vrow">
          <span className="ops-vk">Swap</span>
          <div className="ops-vmid">
            <span className="ops-vv">
              {fmtBytes(swapUsed)} <small>/ {fmtBytes(metrics.swap_total_bytes)}</small>
            </span>
          </div>
        </div>
      )}
      {metrics.fan_rpm && Object.keys(metrics.fan_rpm).length > 0 && (
        <div className="ops-vrow">
          <span className="ops-vk">Fans</span>
          <div className="ops-vmid">
            {/* RPM has no fixed ceiling, so this is a text readout (no meter). */}
            <span className="ops-vv">
              {Object.entries(metrics.fan_rpm)
                .map(([label, rpm]) => `${label} ${rpm}rpm`)
                .join(" · ")}
            </span>
          </div>
        </div>
      )}
    </>
  );
}

// ===== Service group + row, each row carrying its own pullable log tail =====

const LOG_TAIL = 200;
const LEVEL_WORD: Record<Level, string> = {
  ok: "all up",
  off: "all up",
  warn: "degraded",
  bad: "down",
};

function ServiceGroup({
  group,
  chosenEngine,
  memByService,
  onRestart,
  onLifecycle,
}: {
  group: { label: string; items: ContainerStatus[] };
  chosenEngine: string | null;
  memByService: Map<string, number>;
  onRestart: (service: string) => void;
  onLifecycle: (service: string, action: "start" | "stop") => void;
}) {
  const level = groupLevel(group.items, chosenEngine);
  const off = group.items.filter((c) => svcLevel(c, chosenEngine) === "off").length;
  // A group of only switched-off extras is "off", not "all up": nothing in it is running.
  const allOff = off === group.items.length;
  return (
    <OpsCard
      title={group.label}
      bodyClassName="ops-srows"
      headerRight={
        <>
          <span className="ops-gcount">{group.items.length}</span>
          {off > 0 && !allOff && (
            <span className="ops-gstate ops-gstate-off">
              <span className="ops-gdot" />
              {off} off
            </span>
          )}
          <span className={`ops-gstate ops-gstate-${allOff ? "off" : level}`}>
            <span className="ops-gdot" />
            {allOff ? "off" : LEVEL_WORD[level]}
          </span>
        </>
      }
    >
      {group.items.map((c) => (
        <ServiceRow
          key={c.service}
          c={c}
          level={svcLevel(c, chosenEngine)}
          memBytes={memByService.get(c.service) ?? null}
          onRestart={onRestart}
          onLifecycle={onLifecycle}
        />
      ))}
    </OpsCard>
  );
}

function ServiceRow({
  c,
  level,
  memBytes,
  onRestart,
  onLifecycle,
}: {
  c: ContainerStatus;
  level: Level;
  memBytes: number | null;
  onRestart: (service: string) => void;
  onLifecycle: (service: string, action: "start" | "stop") => void;
}) {
  const [open, setOpen] = useState(false);
  const what = SERVICE_WHAT[c.service];
  return (
    <div className="ops-srow">
      <button
        type="button"
        className={`ops-shead${open ? " open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={`ops-sdot ops-sdot-${level}`} />
        <span className="ops-sinfo">
          <span className="ops-sline">
            <span className="ops-snm">{c.service}</span>
            {level === "off" ? (
              <span className="badge off">off</span>
            ) : (
              <>
                <span className={badgeClass(c.state)}>{c.state}</span>
                {c.health && <span className={badgeClass(c.health)}>{c.health}</span>}
              </>
            )}
          </span>
          <span className="ops-smeta">
            {what ?? c.image}
            {level !== "off" &&
              c.started_at &&
              ` · since ${new Date(c.started_at).toLocaleString()}`}
          </span>
        </span>
        {memBytes !== null && <span className="ops-smem">{fmtBytes(memBytes)}</span>}
        <span className="ops-scaret">›</span>
      </button>
      {open && (
        <ServiceBody c={c} memBytes={memBytes} onRestart={onRestart} onLifecycle={onLifecycle} />
      )}
    </div>
  );
}

function ServiceBody({
  c,
  memBytes,
  onRestart,
  onLifecycle,
}: {
  c: ContainerStatus;
  memBytes: number | null;
  onRestart: (service: string) => void;
  onLifecycle: (service: string, action: "start" | "stop") => void;
}) {
  const [lines, setLines] = useState<string[] | null>(null);
  const [follow, setFollow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);
  // A `tail -f` SSE relay never terminates on its own, so a backgrounded app
  // would hold it (and its upstream) open indefinitely. Close it while hidden
  // and re-open on return — the followed log resumes from "now" (lines emitted
  // while hidden aren't replayed), which is fine for a live debug tail.
  const foreground = useForeground();

  // Opening the row pulls this service's tail; the stream attaches only while
  // Follow is on (the old shared LogViewer, now scoped to one service).
  useEffect(() => {
    let cancelled = false;
    api
      .opsLogs(c.service, LOG_TAIL)
      .then((text) => {
        if (!cancelled) setLines(text.split("\n"));
      })
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [c.service]);

  useEffect(() => {
    if (!follow || !foreground) return;
    const source = api.opsLogStream(c.service);
    source.onmessage = (event: MessageEvent<string>) => {
      setLines((prev) => [...(prev ?? []), event.data]);
    };
    source.onerror = () => setError("Log stream disconnected.");
    return () => source.close();
  }, [follow, c.service, foreground]);

  // Auto-scroll so a followed log behaves like `tail -f`.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run on every new line; the effect reads the DOM, not `lines`.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  async function copyLogs() {
    const text = (lines ?? []).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard blocked (insecure context / denied) — leave the button as-is.
      setError("Couldn't copy — clipboard unavailable.");
    }
  }

  return (
    <div className="ops-sbody">
      <div className="ops-kv">
        <span>image</span>
        <span>{c.image}</span>
      </div>
      {c.started_at && (
        <div className="ops-kv">
          <span>uptime since</span>
          <span>{new Date(c.started_at).toLocaleString()}</span>
        </div>
      )}
      {memBytes !== null && (
        <div className="ops-kv">
          <span>memory</span>
          <span>{fmtBytes(memBytes)}</span>
        </div>
      )}

      <div className="ops-logbar">
        <span className="ops-logtitle">Logs · {c.service}</span>
        <label className="ops-follow">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          Follow
        </label>
        <button
          type="button"
          className="ops-copy"
          onClick={() => void copyLogs()}
          disabled={lines === null}
        >
          {copied ? "Copied" : "Copy logs"}
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <pre className="ops-log" ref={logRef} aria-label={`Logs for ${c.service}`}>
        {lines === null ? "loading…" : lines.join("\n")}
      </pre>

      <div className="ops-sactions">
        <button type="button" className="danger ops-srestart" onClick={() => onRestart(c.service)}>
          Restart {c.service}
        </button>
        {c.state === "running" || c.state === "restarting" ? (
          <button
            type="button"
            className="ops-slifecycle"
            onClick={() => onLifecycle(c.service, "stop")}
          >
            Stop
          </button>
        ) : (
          <button
            type="button"
            className="ops-slifecycle"
            onClick={() => onLifecycle(c.service, "start")}
          >
            Start
          </button>
        )}
      </div>
    </div>
  );
}

// ===== History card — time-series graphs over a selectable window =====

const HISTORY_RANGES: MetricRange[] = ["6h", "24h", "7d", "30d", "1y"];

// ===== The panels, as they last described themselves =====
//
// THE PANEL HAS REPORTED RICHLY FOR MONTHS AND NOBODY COULD READ IT. Everything below arrives
// in a telemetry body every fifteen minutes, and until this card the only reader was `grep`
// over the box's structured log — reachable through the debug API and a terminal, neither of
// which the owner has (CLAUDE.md #10). "Is her panel alive, did the update land, is it still
// drawing" were questions he had to hand to somebody with a shell, and "just your update only
// has 0.2.88" is what that cost on a panel that had updated forty minutes earlier.
//
// On Ops rather than beside the messages, because these are the questions asked ABOUT a panel
// rather than through it — next to the update that put the version there.

/** The Panels tile's line, and whether any panel needs a look. */
function panelsGlance(
  panels: PanelStatusOut[] | null,
  error: string | null,
): { word: string; unwell: number } {
  const unwell = (panels ?? []).filter((p) => panelHealth(p.age_s) !== "ok").length;
  const word = error
    ? "unavailable"
    : panels === null
      ? "checking…"
      : panels.length === 0
        ? "none flashed"
        : unwell === 0
          ? `${panels.length} reporting`
          : `${unwell} not reporting`;
  return { word, unwell };
}

// The fleet is fetched by the screen (refetched on every Refresh — the press right after an
// update is the owner asking whether the new version landed), so the tile can say "2 not
// reporting" without the page being open.
function PanelsBody({ panels, error }: { panels: PanelStatusOut[] | null; error: string | null }) {
  return (
    <section className="ops-card">
      {error && <p className="muted ops-vrow-empty">{error}</p>}
      {panels?.length === 0 && (
        <p className="muted ops-vrow-empty">
          No panels flashed against this box yet — the Flash tab on the jpanel screen adds one.
        </p>
      )}
      {/* READ-ONLY, deliberately. This card is where a fault is NOTICED — a version that did not
          move, a unit that has gone quiet — and jpanel is where a panel is named or retired. Two
          places to revoke would be two places to get it wrong, and jpanel is the door the owner
          already thinks of as "the panels in my house". */}
      {panels !== null && panels.length > 0 && (
        <p className="muted ops-panel-note ops-panel-intro">
          Rename or revoke a panel on the <strong>jpanel</strong> screen, Panels tab.
        </p>
      )}
      {panels?.map((p) => {
        const state = panelHealth(p.age_s);
        const concerns = panelConcerns(p.report);
        return (
          <div key={p.device_id} className={`ops-panel-row${state === "ok" ? "" : " bad"}`}>
            <div className="ops-panel-head">
              <span className="ops-panel-name">{p.name}</span>
              {/* Only a display is badged. A pet is the ordinary case and every panel in the
                  house is one, so badging both would put a word on every row that answers a
                  question nobody asked; the badge earns its place by marking the exception. */}
              {p.role === "display" && <span className="ops-panel-role">display</span>}
              <span className="ops-panel-version">{p.version || "—"}</span>
              <span className="ops-panel-seen">{agoLabel(p.age_s)}</span>
            </div>
            {state === "never" ? (
              /* A DIFFERENT FAULT FROM HAVING GONE QUIET, and a different first move: this
                 one was flashed and never came up, so the question is whether it was
                 provisioned against this box at all. */
              <p className="ops-panel-note">Flashed, but has never reported.</p>
            ) : (
              <p className="ops-panel-facts">{panelFacts(p.report).join(" · ") || "no detail"}</p>
            )}
            {concerns.map((c) => (
              <p className="ops-panel-concern" key={c}>
                {c}
              </p>
            ))}
          </div>
        );
      })}
    </section>
  );
}

/** Host settings the app depends on and cannot always apply.
 *
 *  This card exists because the setting that mattered was invisible. Our own installer put
 *  `ttm.pages_limit` at 124 GiB on a 121 GiB box — which DISABLES it, since the GTT
 *  over-commit it refuses can never occur above total RAM — and the product said nothing.
 *  It surfaced weeks later, from reading a shell script, after a freeze that cost a power
 *  cycle. A boot parameter cannot be changed from a phone; being told it is wrong can.
 *
 *  Its Ops tile turns red and names the count when something does not hold: a health panel
 *  nobody opens is not a health panel. */
function HostBody({ data, error }: { data: HostSettings | null; error: string | null }) {
  return (
    <section className="ops-card">
      {error && <p className="muted ops-vrow-empty">{error}</p>}
      {data?.settings.map((c) => (
        <div key={c.key} className={`ops-host-row${c.ok ? "" : " bad"}`}>
          <div className="ops-host-head">
            <span className="ops-host-key">{c.key}</span>
            <span className="ops-host-value">
              {c.current}
              {c.ok ? "" : ` — want ${c.expected}`}
            </span>
          </div>
          {!c.ok && (
            <>
              <p className="ops-host-impact">{c.impact}</p>
              {c.remedy && (
                <p className="muted ops-host-remedy">
                  {/* Named explicitly, because "needs the host" is the difference between
                      something the owner can fix now and something they must plan for. */}
                  {c.needs_host ? "Needs host access: " : ""}
                  {c.remedy}
                </p>
              )}
            </>
          )}
        </div>
      ))}
    </section>
  );
}

/** The range buttons drive the refetch; the resolution note tells the operator raw vs hourly
 * rollup. */
function HistoryBody({ refreshKey }: { refreshKey: number }) {
  const [range, setRange] = useState<MetricRange>("6h");
  const [history, setHistory] = useState<MetricsHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Refetch on a range change AND whenever the top Refresh bumps `refreshKey`, so
  // the graphs stay in step with the rest of the page rather than only updating on
  // mount or a range switch.
  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey is a re-run trigger, not read in the effect
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .opsMetricsHistory(range)
      .then((h) => {
        if (!cancelled) {
          setHistory(h);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [range, refreshKey]);

  const points = history?.points ?? [];
  return (
    <>
      <div className="ops-range">
        {HISTORY_RANGES.map((r) => (
          <button
            key={r}
            type="button"
            className={`ops-range-btn${r === range ? " active" : ""}`}
            aria-pressed={r === range}
            onClick={() => setRange(r)}
          >
            {r}
          </button>
        ))}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {points.length === 0 ? (
        <p className="muted ops-vrow-empty">
          {loading ? "Loading…" : "No samples recorded for this range yet."}
        </p>
      ) : (
        <>
          <TimeSeriesPlot series={serverMetricSeries(points)} />
          {history && (
            <p className="ops-chart-note">
              {`${points.length} ${history.resolution === "raw" ? "30s" : "hourly"} buckets`}
            </p>
          )}
        </>
      )}
    </>
  );
}

// Always open: the graphs are half of what the owner opens Ops to see.
function HistoryCard({ refreshKey }: { refreshKey: number }) {
  return (
    <section className="ops-card" aria-label="History">
      <div className="ops-card-static">History</div>
      <div className="ops-card-body ops-graph-body">
        <HistoryBody refreshKey={refreshKey} />
      </div>
    </section>
  );
}

// ===== Memory card — per-process RAM breakdown (stacked bar + sortable table) =====
// Driven by /ops/metrics `processes` (the supervisor's `docker top`), this is the
// only view that splits the local-llm container into its per-model llama-server
// processes — the whole reason the 120B's footprint is legible here.

type MemItem = { service: string; rss_bytes: number; command: string };

const MEM_GROUPS: { services: string[]; cls: string }[] = [
  { services: ["flash-next", "local-llm", "embed", "comfyui", "tts-stt", "rapidocr"], cls: "ai" },
  { services: ["jcode"], cls: "code" },
  { services: ["api", "worker", "supervisor", "db", "postgres", "web"], cls: "core" },
];
function memGroup(service: string): string {
  return MEM_GROUPS.find((g) => g.services.includes(service))?.cls ?? "infra";
}
// A llama-server argv carries the model path; surface just that as the row's detail.
function procLabel(p: MemItem): { name: string; detail: string } {
  const model = /([\w.-]+)\.gguf/.exec(p.command);
  if (model) {
    const dir = p.command.split("/").slice(-2, -1)[0];
    return { name: "llama-server", detail: dir || model[1] || p.service };
  }
  const head = p.command.split(/\s+/)[0]?.split("/").pop();
  return { name: head || p.service, detail: p.service };
}

function MemoryCard({
  metrics,
  onRefresh,
  busy,
}: {
  metrics: OpsMetrics | null;
  onRefresh: () => void;
  busy: boolean;
}) {
  const [sort, setSort] = useState<"rss" | "name">("rss");
  const [view, setView] = useState<"table" | "donut">("table");

  if (!metrics) {
    return (
      <section className="ops-card">
        <p className="muted ops-vrow-empty">metrics unavailable.</p>
      </section>
    );
  }

  // `free`/`htop` view: reclaimable cache is AVAILABLE, so `used` is only the
  // non-reclaimable occupancy — process RSS + iGPU device memory + kernel/slab.
  // Cache and free are both available; cache just happens to hold model files.
  const { total, used, cache, free } = memParts(metrics);
  const pct = total > 0 ? Math.round((used / total) * 100) : 0;
  // Per-process when the supervisor offers it; else fall back to per-container.
  const items: MemItem[] =
    metrics.processes.length > 0
      ? metrics.processes.map((p) => ({
          service: p.service,
          rss_bytes: p.rss_bytes,
          command: p.command,
        }))
      : metrics.containers.map((c) => ({
          service: c.service,
          rss_bytes: c.mem_bytes,
          command: "",
        }));
  const accounted = items.reduce((s, p) => s + p.rss_bytes, 0);

  // Split `used` for the bar/donut: process RSS + iGPU (GTT/VRAM, no per-process
  // RSS) + the kernel/slab remainder. These three sum to `used`.
  const gpu = metrics.gpu_mem
    ? metrics.gpu_mem.gtt_used_bytes + metrics.gpu_mem.vram_used_bytes
    : 0;
  const apps = Math.min(accounted, used);
  const kernel = Math.max(0, used - apps - gpu);

  const byRss = [...items].sort((a, b) => b.rss_bytes - a.rss_bytes);
  const rows =
    sort === "rss"
      ? byRss
      : [...items].sort((a, b) => (a.service + a.command).localeCompare(b.service + b.command));
  const maxRss = byRss[0]?.rss_bytes ?? 1;

  // Donut composition over the whole total (free is the uncovered remainder), as
  // cumulative dash offsets: process groups, then iGPU, kernel, and cache slices.
  const groupTotals = new Map<string, number>();
  for (const p of items)
    groupTotals.set(memGroup(p.service), (groupTotals.get(memGroup(p.service)) ?? 0) + p.rss_bytes);
  if (gpu > 0) groupTotals.set("gpu", gpu);
  if (kernel > 0) groupTotals.set("kernel", kernel);
  if (cache > 0) groupTotals.set("cache", cache);
  let offset = 0;
  const arcs = [...groupTotals.entries()].map(([cls, v]) => {
    const len = total > 0 ? (v / total) * 100 : 0;
    const arc = { cls, dash: `${len} ${100 - len}`, off: -offset };
    offset += len;
    return arc;
  });

  return (
    <section className="ops-card ops-mem">
      <div className="ops-mem-head">
        <span className="ops-mem-pct">{pct}%</span>
        <span className="ops-mem-cap">
          {fmtBytes(used)} used <small>/ {fmtBytes(total)}</small>
        </span>
      </div>
      <div className="ops-mem-stack">
        {byRss.map((p, i) => (
          <span
            key={`${p.service}-${i}`}
            className={`ops-mem-seg g-${memGroup(p.service)}`}
            style={{ width: `${(p.rss_bytes / total) * 100}%` }}
            title={`${procLabel(p).name} · ${fmtBytes(p.rss_bytes)}`}
          />
        ))}
        {gpu > 0 && (
          <span
            className="ops-mem-seg g-gpu"
            style={{ width: `${(gpu / total) * 100}%` }}
            title={`iGPU ${fmtBytes(gpu)} — loaded model weights in GTT/VRAM (no per-process RSS)`}
          />
        )}
        <span
          className="ops-mem-seg g-kernel"
          style={{ width: `${(kernel / total) * 100}%` }}
          title={`kernel & other ${fmtBytes(kernel)}`}
        />
        <span
          className="ops-mem-seg g-cache"
          style={{ width: `${(cache / total) * 100}%` }}
          title={`cache ${fmtBytes(cache)} — reclaimable (freed on demand)`}
        />
      </div>

      <div className="ops-mem-tools">
        <div className="ops-seg" role="tablist">
          <button
            type="button"
            className={view === "table" ? "on" : ""}
            onClick={() => setView("table")}
          >
            Table
          </button>
          <button
            type="button"
            className={view === "donut" ? "on" : ""}
            onClick={() => setView("donut")}
          >
            Donut
          </button>
        </div>
        <button type="button" className="ops-mem-refresh" onClick={onRefresh} disabled={busy}>
          {busy ? "Refreshing…" : "↻ Refresh"}
        </button>
      </div>

      {view === "donut" ? (
        <div className="ops-mem-donutwrap">
          <svg
            className="ops-mem-donut"
            viewBox="0 0 42 42"
            role="img"
            aria-label="Memory by group"
          >
            <title>Memory by group</title>
            <circle className="ring-bg" cx="21" cy="21" r="15.915" />
            {arcs.map((a) => (
              <circle
                key={a.cls}
                className={`ring g-${a.cls}`}
                cx="21"
                cy="21"
                r="15.915"
                strokeDasharray={a.dash}
                strokeDashoffset={a.off}
              />
            ))}
            <text className="ops-mem-donut-cap" x="21" y="20.5" textAnchor="middle">
              {fmtBytes(used)}
            </text>
            <text className="ops-mem-donut-cap2" x="21" y="26" textAnchor="middle">
              of {fmtBytes(total)}
            </text>
          </svg>
        </div>
      ) : (
        <table className="ops-mem-table">
          <thead>
            <tr>
              <th>
                <button
                  type="button"
                  className="ops-mem-sort"
                  onClick={() => setSort("name")}
                  aria-pressed={sort === "name"}
                >
                  Process
                </button>
              </th>
              <th className="r">
                <button
                  type="button"
                  className="ops-mem-sort"
                  onClick={() => setSort("rss")}
                  aria-pressed={sort === "rss"}
                >
                  RSS {sort === "rss" ? "▾" : ""}
                </button>
              </th>
              <th className="r">Share</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p, i) => {
              const { name, detail } = procLabel(p);
              return (
                <tr key={`${p.service}-${i}`}>
                  <td>
                    <span className="ops-mem-pname">
                      <span className={`ops-mem-sw g-${memGroup(p.service)}`} />
                      <span>
                        <span className="ops-mem-nm">{name}</span>
                        <span className="ops-mem-svc">
                          {p.service}
                          {detail && detail !== p.service ? ` · ${detail}` : ""}
                        </span>
                      </span>
                    </span>
                  </td>
                  <td className="r">
                    <span className="ops-mem-spark">
                      <i
                        className={`g-${memGroup(p.service)}`}
                        style={{ width: `${(p.rss_bytes / maxRss) * 100}%` }}
                      />
                    </span>
                    {fmtBytes(p.rss_bytes)}
                  </td>
                  <td className="r ops-mem-share">
                    {used > 0
                      ? ((p.rss_bytes / used) * 100).toFixed(p.rss_bytes / used < 0.01 ? 1 : 0)
                      : 0}
                    %
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <p className="ops-mem-foot">
        <b>{fmtBytes(used)}</b> used (<b>{fmtBytes(accounted)}</b> {items.length}{" "}
        {metrics.processes.length > 0 ? "processes" : "containers"}
        {gpu > 0 && (
          <>
            {" · "}
            <b>{fmtBytes(gpu)}</b> iGPU
          </>
        )}
        {" · "}
        <b>{fmtBytes(kernel)}</b> kernel){" · "}
        <b>{fmtBytes(cache + free)}</b> available
        {cache > 0 && (
          <>
            {" "}
            (<b>{fmtBytes(cache)}</b> reclaimable cache)
          </>
        )}
      </p>
      <p className="ops-mem-note">
        Reclaimable <b>cache</b> (model files read from disk) counts as available — it&apos;s freed
        the instant anything needs the RAM. <b>iGPU</b> memory holding loaded model weights is real
        usage but has no per-process RSS, so it shows as its own slice, not in the rows above.
      </p>
    </section>
  );
}

// Ops opens on live vitals, the one Update, and the graphs; everything else sits behind a
// launcher-style tile that pushes its own page (docs/mocks/ops-launcher/ops-launcher.html).
type OpsPage = "services" | "memory" | "engine" | "panels" | "host" | "storage";

const PAGE_TITLE: Record<OpsPage, string> = {
  services: "Services",
  memory: "Memory",
  engine: "Engine",
  panels: "Panels",
  host: "Host",
  storage: "Storage",
};

interface OpsTile {
  id: string;
  title: string;
  icon: ReactNode;
  sub: string;
  /** A longer line for the tile's label, where the tile itself has room for one word. */
  detail?: string;
  /** A live state dot shown even when all is well — Minecraft's, the same dot its launcher
   *  tile carries, so "running" reads green in both places. */
  dot?: McLevel | undefined;
  tone: "" | "warn" | "bad";
  onOpen: () => void;
}

// The Minecraft tile's glance doesn't need the screen's 5 s beat; it only has to be current enough
// that "who's on" isn't stale when the owner looks.
const MC_POLL_MS = 15_000;

interface McSnapshot {
  status: MinecraftStatus | null;
  version: MinecraftVersion | null;
  error: string | null;
}

export function OpsScreen({ onOpenMinecraft }: { onOpenMinecraft?: () => void } = {}) {
  const [containers, setContainers] = useState<ContainerStatus[] | null>(null);
  const [metrics, setMetrics] = useState<OpsMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Bumped on every top-level refresh so self-fetching sub-cards (the History
  // graphs, which own their range + fetch) refetch too — the top Refresh button
  // means "refresh everything", not just the status + metrics fetched here.
  const [refreshKey, setRefreshKey] = useState(0);
  // The Runs surface (Direction C) is an Ops sub-screen: it slides over Ops and
  // its back chevron returns here, matching the mock.
  const [showRuns, setShowRuns] = useState(false);
  // The tile whose page is pushed over the grid, if any.
  const [page, setPage] = useState<OpsPage | null>(null);
  const foreground = useForeground();
  const engine = useEngineSnapshot();

  // Panels and host settings are read here rather than in their pages so their tiles can say
  // what needs a look without being opened.
  const [panels, setPanels] = useState<PanelStatusOut[] | null>(null);
  const [panelsError, setPanelsError] = useState<string | null>(null);
  const [host, setHost] = useState<HostSettings | null>(null);
  const [hostError, setHostError] = useState<string | null>(null);

  // Minecraft has its own screen, but its container is also stoppable from here — the
  // service row and Restart all — so Ops keeps a snapshot to warn before bouncing players.
  const [mc, setMc] = useState<McSnapshot>({ status: null, version: null, error: null });
  const mcRef = useRef(mc);
  mcRef.current = mc;
  const [mcConfirm, setMcConfirm] = useState<{
    spec: ConfirmSpec;
    run: () => Promise<void>;
  } | null>(null);

  /** A fresh Minecraft read for a confirm decision, falling back to the last one. */
  const loadMc = useCallback(async (): Promise<McSnapshot | null> => {
    try {
      const [status, version] = await Promise.all([
        api.minecraftStatus(),
        api.minecraftVersion(false).catch(() => mcRef.current.version),
      ]);
      const next = { status, version, error: null };
      setMc(next);
      return next;
    } catch (err) {
      setMc((m) => ({ ...m, error: errorMessage(err) }));
      return mcRef.current.status ? mcRef.current : null;
    }
  }, []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey is the "refresh everything" signal.
  useEffect(() => {
    if (!foreground) return;
    void loadMc();
    const id = setInterval(() => void loadMc(), MC_POLL_MS);
    return () => clearInterval(id);
  }, [foreground, loadMc, refreshKey]);

  const refresh = useCallback(async () => {
    setBusy(true);
    setError(null);
    setRefreshKey((k) => k + 1);
    try {
      setContainers((await api.opsStatus()).containers);
      setMetrics(await api.opsMetrics());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey is the "refresh everything" signal.
  useEffect(() => {
    let cancelled = false;
    api
      .panelStatus()
      .then((r) => {
        if (!cancelled) {
          setPanels(r.panels);
          setPanelsError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setPanelsError(errorMessage(err));
      });
    api
      .opsHostSettings()
      .then((r) => {
        if (!cancelled) {
          setHost(r);
          setHostError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setHostError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  // The engine banner's "Details" (and a recovery the banner arms) lands on the Engine page.
  useEffect(() => {
    if (!engine.focus && engine.armed === null) return;
    setPage("engine");
    clearEngineFocus();
  }, [engine.focus, engine.armed]);

  /** Act on the Minecraft container through its own API, so the world is saved first. */
  const runMc = useCallback(
    async (act: () => Promise<unknown>) => {
      setError(null);
      try {
        await act();
      } catch (err) {
        setError(errorMessage(err));
      }
      await refresh();
      void loadMc();
    },
    [refresh, loadMc],
  );

  const restart = useCallback(
    async (service: string) => {
      // Neither path may bounce players silently: the confirm is the Minecraft screen's own.
      if (service === "all" || service === MC_SERVICE) {
        const snap = await loadMc();
        if (snap) {
          const ctx = confirmContext(snap.status, snap.version);
          if (service === MC_SERVICE) {
            const spec = confirmFor("restart", ctx);
            const run = () => runMc(() => api.minecraftRestart());
            if (spec) setMcConfirm({ spec, run });
            else await run();
            return;
          }
          const spec = confirmFor("all", ctx);
          if (spec) {
            setMcConfirm({ spec, run: () => runMc(() => api.opsRestart("all")) });
            return;
          }
        }
      }
      const target = service === "all" ? "ALL services" : service;
      if (!window.confirm(`Restart ${target}?`)) return;
      setError(null);
      try {
        await api.opsRestart(service);
        await refresh();
      } catch (err) {
        setError(errorMessage(err));
      }
    },
    [refresh, loadMc, runMc],
  );

  // Power a single container off/on. Stop is disruptive, so it confirms; Start is safe.
  const lifecycle = useCallback(
    async (service: string, action: "start" | "stop") => {
      if (action === "stop" && service === MC_SERVICE) {
        const snap = await loadMc();
        if (snap) {
          const spec = confirmFor("stop", confirmContext(snap.status, snap.version));
          const run = () => runMc(() => api.minecraftStop());
          if (spec) setMcConfirm({ spec, run });
          else await run();
          return;
        }
      }
      if (action === "stop" && !window.confirm(`Stop ${service}?`)) return;
      setError(null);
      try {
        await (action === "stop" ? api.opsStop(service) : api.opsStart(service));
        await refresh();
      } catch (err) {
        setError(errorMessage(err));
      }
    },
    [refresh, loadMc, runMc],
  );

  const groups = groupContainers(containers ?? []);
  const memByService = new Map((metrics?.containers ?? []).map((x) => [x.service, x.mem_bytes]));
  const es = engine.state;
  const chosenEngine = es ? (es.services[es.desired]?.service ?? null) : null;

  // The banner names only real trouble: an "off" service never raises it.
  const troubled = (containers ?? []).filter((c) => {
    const l = svcLevel(c, chosenEngine);
    return l === "bad" || l === "warn";
  });
  const troubleLevel = troubled.reduce<Level>((w, c) => worse(w, svcLevel(c, chosenEngine)), "ok");
  const offCount = (containers ?? []).filter((c) => svcLevel(c, chosenEngine) === "off").length;

  const mem = metrics ? memParts(metrics) : null;
  const memPct = mem && mem.total > 0 ? Math.round((mem.used / mem.total) * 100) : null;
  const engineTile = engineGlance(es, engine.error, engine.dismissed);
  const panelTile = panelsGlance(panels, panelsError);
  const hostBad = host ? host.settings.filter((c) => !c.ok).length : 0;
  const showMc =
    onOpenMinecraft !== undefined &&
    mc.status?.container !== null &&
    (mc.status !== null || mc.error);
  // The launcher tile's word (it flags a waiting update), with the glance's line — who's on, or
  // the lockout — carried in the tile's label.
  const mcGlance = glanceOf(mc.status, mc.version, mc.error);
  const mcTile = tileOf(mc.status, mc.version);
  const mcTone: OpsTile["tone"] =
    mcGlance.tone || (mcGlance.level === "bad" || mcGlance.level === "warn" ? mcGlance.level : "");

  const tiles: OpsTile[] = [
    {
      id: "services",
      title: "Services",
      icon: <LayersIcon size={24} />,
      sub:
        containers === null
          ? "checking…"
          : troubled.length > 0
            ? `${troubled.length} ${troubleLevel === "bad" ? "down" : "degraded"}`
            : `${containers.length - offCount} up${offCount > 0 ? ` · ${offCount} off` : ""}`,
      tone: troubled.length > 0 ? (troubleLevel === "bad" ? "bad" : "warn") : "",
      onOpen: () => setPage("services"),
    },
    {
      id: "memory",
      title: "Memory",
      icon: <MemoryIcon size={24} />,
      sub: memPct === null ? "unavailable" : `${memPct}% used`,
      tone: memPct !== null && memPct >= 95 ? "bad" : memPct !== null && memPct >= 85 ? "warn" : "",
      onOpen: () => setPage("memory"),
    },
    {
      id: "engine",
      title: "Engine",
      icon: <BotIcon size={24} />,
      sub: engineTile.word,
      tone: engineTile.tone,
      onOpen: () => setPage("engine"),
    },
    ...(showMc && onOpenMinecraft
      ? [
          {
            id: "minecraft",
            title: "Minecraft",
            icon: <CubeIcon size={24} />,
            sub: mcTile?.word ?? mcGlance.word,
            detail: mcGlance.meta,
            tone: mcTone,
            dot: mcTile?.level,
            onOpen: onOpenMinecraft,
          },
        ]
      : []),
    {
      id: "panels",
      title: "Panels",
      icon: <MonitorIcon size={24} />,
      sub: panelTile.word,
      tone: panelTile.unwell > 0 ? "warn" : "",
      onOpen: () => setPage("panels"),
    },
    {
      id: "host",
      title: "Host",
      icon: <ShieldIcon size={24} />,
      sub: hostError
        ? "unavailable"
        : host === null
          ? "checking…"
          : hostBad === 0
            ? "all good"
            : `${hostBad} ${hostBad === 1 ? "issue" : "issues"}`,
      tone: hostBad > 0 ? "bad" : "",
      onOpen: () => setPage("host"),
    },
    {
      id: "runs",
      title: "Runs",
      icon: <ClockIcon size={24} />,
      sub: "workflows",
      tone: "",
      onOpen: () => setShowRuns(true),
    },
    {
      id: "storage",
      title: "Storage",
      icon: <DatabaseIcon size={24} />,
      sub: metrics?.db ? `DB ${fmtBytes(metrics.db.db_size_bytes)}` : "database",
      tone: "",
      onOpen: () => setPage("storage"),
    },
  ];

  return (
    <section className="ops">
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <VitalsCard metrics={metrics} onRefresh={() => void refresh()} busy={busy} />

      {troubled.length > 0 && (
        <button
          type="button"
          className={`ops-attn ops-attn-${troubleLevel}`}
          onClick={() => setPage("services")}
        >
          <span className={`ops-sdot ops-sdot-${troubleLevel}`} />
          <span className="ops-attn-text">
            <b>
              {troubled.map((c) => c.service).join(", ")}{" "}
              {troubleLevel === "bad" ? "down" : "degraded"}
            </b>
          </span>
          <span className="ops-scaret">›</span>
        </button>
      )}

      <section className="ops-card ops-update-card" aria-label="Server update">
        <UpdateControl />
      </section>

      <HistoryCard refreshKey={refreshKey} />

      <div className="tile-grid ops-tiles">
        {tiles.map((t) => (
          <div key={t.id} className="tile-slot">
            <button
              type="button"
              className="tile"
              onClick={t.onOpen}
              aria-label={`${t.title}: ${t.sub}${t.detail ? ` — ${t.detail}` : ""}`}
            >
              {t.dot ? (
                <span className={`mc-tile-dot ${t.dot}`} aria-hidden="true" />
              ) : (
                t.tone && <span className={`ops-tile-dot ${t.tone}`} aria-hidden="true" />
              )}
              <span className="tile-icon">{t.icon}</span>
              <span className="tile-title">{t.title}</span>
              <span className={`tile-sub${t.tone ? ` ${t.tone}` : ""}`}>{t.sub}</span>
            </button>
          </div>
        ))}
      </div>

      {page !== null && (
        <PageLayer title={PAGE_TITLE[page]} onBack={() => setPage(null)}>
          {page === "services" &&
            (containers === null && !error ? (
              <p className="muted">Loading status…</p>
            ) : (
              <>
                {groups.map((g) => (
                  <ServiceGroup
                    key={g.label}
                    group={g}
                    chosenEngine={chosenEngine}
                    memByService={memByService}
                    onRestart={restart}
                    onLifecycle={lifecycle}
                  />
                ))}
                <section className="ops-card ops-restart-all">
                  <span className="ops-restart-all-text">
                    <b>Restart all</b>
                    <span className="muted">
                      Restarts every running service. The app drops briefly.
                    </span>
                  </span>
                  <button
                    type="button"
                    className="danger"
                    onClick={() => void restart("all")}
                    disabled={containers === null}
                  >
                    Restart all
                  </button>
                </section>
              </>
            ))}
          {page === "memory" && <MemoryCard metrics={metrics} onRefresh={refresh} busy={busy} />}
          {page === "engine" && (
            <>
              <LocalEngineSection />
              <section className="ops-card ops-pad" aria-label="Prompt cache">
                <PromptCacheControls />
              </section>
            </>
          )}
          {page === "panels" && <PanelsBody panels={panels} error={panelsError} />}
          {page === "host" && <HostBody data={host} error={hostError} />}
          {page === "storage" && (
            <section className="ops-card">
              <StorageRows metrics={metrics} />
            </section>
          )}
        </PageLayer>
      )}

      {showRuns && <RunsScreen onClose={() => setShowRuns(false)} />}

      {mcConfirm && (
        <Dialog
          title={mcConfirm.spec.title}
          confirmLabel={mcConfirm.spec.confirmLabel}
          tone={mcConfirm.spec.tone}
          onCancel={() => setMcConfirm(null)}
          onConfirm={() => {
            const { run } = mcConfirm;
            setMcConfirm(null);
            void run();
          }}
        >
          {mcConfirm.spec.body}
        </Dialog>
      )}
    </section>
  );
}
