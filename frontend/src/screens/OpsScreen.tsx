import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type ContainerStatus,
  type OpsMetrics,
  type UpdateStatus,
  api,
} from "../api/client";
import { useForeground, useForegroundRef } from "../visibility";
import { RunsScreen } from "./RunsScreen";

function fmtBytes(n: number): string {
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(1)} GB`;
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(0)} MB`;
  return `${(n / 1024).toFixed(0)} KB`;
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

type Level = "ok" | "warn" | "bad";
const LEVEL_RANK: Record<Level, number> = { ok: 0, warn: 1, bad: 2 };

function svcLevel(c: ContainerStatus): Level {
  if (c.state === "exited" || c.state === "dead") return "bad";
  if (c.health === "unhealthy") return "bad";
  if (c.health === "starting" || c.state === "restarting" || c.state === "created") return "warn";
  if (c.state === "running") return c.health === null || c.health === "healthy" ? "ok" : "warn";
  return "warn";
}

function worse(a: Level, b: Level): Level {
  return LEVEL_RANK[b] > LEVEL_RANK[a] ? b : a;
}

/** Services are grouped by role so the list stays scannable as the stack
 * grows (B3 redesign). Grouping is frontend-only — the backend status payload
 * is flat; anything unrecognized falls into a trailing "Other" group. */
const SERVICE_GROUPS: { label: string; services: string[] }[] = [
  { label: "Core", services: ["api", "worker", "supervisor", "web", "db", "postgres"] },
  { label: "AI", services: ["local-llm", "embed"] },
  { label: "Infra", services: ["proxy", "searxng", "cloudflared"] },
];

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

// ===== Collapsible card — the shared disclosure shell for every Ops section =====

/** `headerRight` shows in the header whether open or closed (group counts);
 * `summaryCollapsed` shows only while collapsed (the System recap). The body
 * is mounted only when open, so collapsed groups never fetch their logs. */
function OpsCard({
  title,
  defaultOpen = false,
  headerRight,
  summaryCollapsed,
  bodyClassName,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  headerRight?: ReactNode;
  summaryCollapsed?: ReactNode;
  bodyClassName?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="ops-card">
      <button
        type="button"
        className={`ops-card-head${open ? " open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="ops-card-title">{title}</span>
        <span className="ops-card-right">
          {!open && summaryCollapsed}
          {headerRight}
        </span>
        <span className="ops-card-caret">›</span>
      </button>
      {open && (
        <div className={`ops-card-body${bodyClassName ? ` ${bodyClassName}` : ""}`}>{children}</div>
      )}
    </section>
  );
}

// ===== Server update — folded into the System card's Load row (owner request) =====

type UpdatePhase =
  | { step: "idle" }
  | { step: "confirm" }
  | { step: "running"; log: string; unreachable: boolean }
  | { step: "done"; ok: boolean; log: string };

const UPDATE_POLL_MS = 3000;

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
            Update server
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

// ===== System card — the four vitals + the embedded server update =====

function SystemCard({ metrics }: { metrics: OpsMetrics | null }) {
  let summary = "metrics unavailable";
  if (metrics) {
    const memPct = Math.round(
      ((metrics.mem_total_bytes - metrics.mem_available_bytes) / metrics.mem_total_bytes) * 100,
    );
    const diskPct = Math.round(
      ((metrics.disk_total_bytes - metrics.disk_free_bytes) / metrics.disk_total_bytes) * 100,
    );
    const gpu =
      metrics.gpu_busy_percent != null ? `gpu ${Math.round(metrics.gpu_busy_percent)}% · ` : "";
    const fanRpms = metrics.fan_rpm ? Object.values(metrics.fan_rpm) : [];
    const fan = fanRpms.length > 0 ? `fan ${Math.max(...fanRpms)}rpm · ` : "";
    summary = `mem ${memPct}% · disk ${diskPct}% · ${gpu}${fan}load ${metrics.load_1m.toFixed(2)} · up ${fmtUptime(metrics.uptime_seconds)}`;
  }
  return (
    <OpsCard
      title="System"
      defaultOpen
      summaryCollapsed={<span className="ops-card-summary">{summary}</span>}
    >
      {metrics === null ? (
        <p className="muted ops-vrow-empty">metrics unavailable.</p>
      ) : (
        <SystemRows metrics={metrics} />
      )}
    </OpsCard>
  );
}

function SystemRows({ metrics }: { metrics: OpsMetrics }) {
  const memUsed = metrics.mem_total_bytes - metrics.mem_available_bytes;
  const diskUsed = metrics.disk_total_bytes - metrics.disk_free_bytes;
  const swapUsed = metrics.swap_total_bytes - metrics.swap_free_bytes;
  return (
    <>
      <div className="ops-vrow">
        <span className="ops-vk">Memory</span>
        <div className="ops-vmid">
          <span className="ops-vv">
            {fmtBytes(memUsed)} <small>/ {fmtBytes(metrics.mem_total_bytes)}</small>
          </span>
          <Meter used={memUsed} total={metrics.mem_total_bytes} />
        </div>
        {metrics.swap_total_bytes > 0 && (
          <span className="ops-vend">swap {fmtBytes(swapUsed)}</span>
        )}
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
      {metrics.gpu_busy_percent != null && (
        <div className="ops-vrow">
          <span className="ops-vk">GPU</span>
          <div className="ops-vmid">
            <span className="ops-vv">{Math.round(metrics.gpu_busy_percent)}%</span>
            <Meter used={metrics.gpu_busy_percent} total={100} tone="util" />
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
      <div className="ops-vrow ops-vrow-load">
        <div className="ops-vrow-line">
          <span className="ops-vk">Load</span>
          <div className="ops-vmid">
            <span className="ops-vv">
              {metrics.load_1m.toFixed(2)} · {metrics.load_5m.toFixed(2)} ·{" "}
              {metrics.load_15m.toFixed(2)}
            </span>
            <span className="ops-vsub">up {fmtUptime(metrics.uptime_seconds)}</span>
          </div>
        </div>
        <UpdateControl />
      </div>
    </>
  );
}

// ===== Service group + row, each row carrying its own pullable log tail =====

const LOG_TAIL = 200;

function ServiceGroup({
  group,
  memByService,
  onRestart,
}: {
  group: { label: string; items: ContainerStatus[] };
  memByService: Map<string, number>;
  onRestart: (service: string) => void;
}) {
  const level = group.items.reduce<Level>((w, c) => worse(w, svcLevel(c)), "ok");
  const label = level === "ok" ? "all up" : level === "warn" ? "degraded" : "down";
  return (
    <OpsCard
      title={group.label}
      bodyClassName="ops-srows"
      headerRight={
        <>
          <span className="ops-gcount">
            {group.items.length} {group.items.length === 1 ? "service" : "services"}
          </span>
          <span className={`ops-gstate ops-gstate-${level}`}>
            <span className="ops-gdot" />
            {label}
          </span>
        </>
      }
    >
      {group.items.map((c) => (
        <ServiceRow
          key={c.service}
          c={c}
          memBytes={memByService.get(c.service) ?? null}
          onRestart={onRestart}
        />
      ))}
    </OpsCard>
  );
}

function ServiceRow({
  c,
  memBytes,
  onRestart,
}: {
  c: ContainerStatus;
  memBytes: number | null;
  onRestart: (service: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="ops-srow">
      <button
        type="button"
        className={`ops-shead${open ? " open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={`ops-sdot ops-sdot-${svcLevel(c)}`} />
        <span className="ops-sinfo">
          <span className="ops-sline">
            <span className="ops-snm">{c.service}</span>
            <span className={badgeClass(c.state)}>{c.state}</span>
            {c.health && <span className={badgeClass(c.health)}>{c.health}</span>}
          </span>
          <span className="ops-smeta">
            {c.image}
            {c.started_at && ` · since ${new Date(c.started_at).toLocaleString()}`}
          </span>
        </span>
        {memBytes !== null && <span className="ops-smem">{fmtBytes(memBytes)}</span>}
        <span className="ops-scaret">›</span>
      </button>
      {open && <ServiceBody c={c} memBytes={memBytes} onRestart={onRestart} />}
    </div>
  );
}

function ServiceBody({
  c,
  memBytes,
  onRestart,
}: {
  c: ContainerStatus;
  memBytes: number | null;
  onRestart: (service: string) => void;
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

      <button type="button" className="danger ops-srestart" onClick={() => onRestart(c.service)}>
        Restart {c.service}
      </button>
    </div>
  );
}

export function OpsScreen() {
  const [containers, setContainers] = useState<ContainerStatus[] | null>(null);
  const [metrics, setMetrics] = useState<OpsMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The Runs surface (Direction C) is an Ops sub-screen: it slides over Ops and
  // its back chevron returns here, matching the mock.
  const [showRuns, setShowRuns] = useState(false);

  const refresh = useCallback(async () => {
    setBusy(true);
    setError(null);
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

  const restart = useCallback(
    async (service: string) => {
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
    [refresh],
  );

  const groups = groupContainers(containers ?? []);
  const memByService = new Map((metrics?.containers ?? []).map((x) => [x.service, x.mem_bytes]));

  return (
    <section className="ops">
      <header className="ops-header">
        <h2>Ops</h2>
        <div className="ops-actions">
          <button type="button" onClick={() => setShowRuns(true)}>
            Runs
          </button>
          <button type="button" onClick={refresh} disabled={busy}>
            {busy ? "Refreshing…" : "Refresh"}
          </button>
          <button
            type="button"
            className="danger"
            onClick={() => void restart("all")}
            disabled={containers === null}
          >
            Restart all
          </button>
        </div>
      </header>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <SystemCard metrics={metrics} />

      {containers === null && !error ? (
        <p className="muted">Loading status…</p>
      ) : (
        groups.map((g) => (
          <ServiceGroup key={g.label} group={g} memByService={memByService} onRestart={restart} />
        ))
      )}

      {showRuns && <RunsScreen onClose={() => setShowRuns(false)} />}
    </section>
  );
}
