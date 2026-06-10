import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type ContainerStatus,
  type OpsMetrics,
  type UpdateStatus,
  api,
} from "../api/client";

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

function Meter({ used, total }: { used: number; total: number }) {
  const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
  const level = pct > 92 ? "bad" : pct > 80 ? "warn" : "ok";
  return (
    <div className="meter">
      <div className={`meter-fill meter-${level}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function MetricsGrid({ metrics }: { metrics: OpsMetrics }) {
  const memUsed = metrics.mem_total_bytes - metrics.mem_available_bytes;
  const diskUsed = metrics.disk_total_bytes - metrics.disk_free_bytes;
  const swapUsed = metrics.swap_total_bytes - metrics.swap_free_bytes;
  return (
    <ul className="container-list metrics-grid">
      <li className="container-row">
        <div className="container-main">
          <span className="service-name">Memory</span>
        </div>
        <span className="metric-value">
          {fmtBytes(memUsed)} / {fmtBytes(metrics.mem_total_bytes)}
        </span>
        <Meter used={memUsed} total={metrics.mem_total_bytes} />
        {metrics.swap_total_bytes > 0 && (
          <span className="container-meta muted">swap {fmtBytes(swapUsed)} used</span>
        )}
      </li>
      <li className="container-row">
        <div className="container-main">
          <span className="service-name">Disk</span>
        </div>
        <span className="metric-value">
          {fmtBytes(diskUsed)} / {fmtBytes(metrics.disk_total_bytes)}
        </span>
        <Meter used={diskUsed} total={metrics.disk_total_bytes} />
      </li>
      <li className="container-row">
        <div className="container-main">
          <span className="service-name">Database</span>
        </div>
        {metrics.db ? (
          <>
            <span className="metric-value">{fmtBytes(metrics.db.db_size_bytes)}</span>
            <span className="container-meta muted">
              {metrics.db.note_count} notes · {metrics.db.attachment_count} files
              {metrics.blobs ? ` · ${fmtBytes(metrics.blobs.total_bytes)} blobs` : ""}
            </span>
          </>
        ) : (
          <span className="container-meta muted">unavailable</span>
        )}
      </li>
      <li className="container-row">
        <div className="container-main">
          <span className="service-name">Load</span>
        </div>
        <span className="metric-value">
          {metrics.load_1m.toFixed(2)} · {metrics.load_5m.toFixed(2)} ·{" "}
          {metrics.load_15m.toFixed(2)}
        </span>
        <span className="container-meta muted">up {fmtUptime(metrics.uptime_seconds)}</span>
      </li>
    </ul>
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

type UpdatePhase =
  | { step: "idle" }
  | { step: "confirm" }
  | { step: "running"; log: string; unreachable: boolean }
  | { step: "done"; ok: boolean; log: string };

const UPDATE_POLL_MS = 3000;

function UpdateCard() {
  const [phase, setPhase] = useState<UpdatePhase>({ step: "idle" });
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (timer.current !== null) clearInterval(timer.current);
    timer.current = null;
  }, []);
  useEffect(() => stopPolling, [stopPolling]);

  const poll = useCallback(async () => {
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
  }, [stopPolling]);

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
    <section className="ops-update">
      <h3>Server update</h3>
      {phase.step === "idle" && (
        <button type="button" onClick={() => setPhase({ step: "confirm" })}>
          Update server
        </button>
      )}
      {phase.step === "confirm" && (
        <button
          type="button"
          className="danger"
          onClick={() => void start()}
          onBlur={() => setPhase({ step: "idle" })}
        >
          Tap again to update — pulls latest main, rebuilds, restarts
        </button>
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
    </section>
  );
}

export function OpsScreen() {
  const [containers, setContainers] = useState<ContainerStatus[] | null>(null);
  const [metrics, setMetrics] = useState<OpsMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

  async function restart(service: string) {
    const target = service === "all" ? "ALL services" : service;
    if (!window.confirm(`Restart ${target}?`)) return;
    setError(null);
    try {
      await api.opsRestart(service);
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  const services = containers?.map((c) => c.service) ?? [];

  return (
    <section className="ops">
      <header className="ops-header">
        <h2>Ops</h2>
        <div className="ops-actions">
          <button type="button" onClick={refresh} disabled={busy}>
            {busy ? "Refreshing…" : "Refresh"}
          </button>
          <button
            type="button"
            className="danger"
            onClick={() => restart("all")}
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

      {metrics !== null && <MetricsGrid metrics={metrics} />}
      {containers === null && !error ? (
        <p className="muted">Loading status…</p>
      ) : (
        <ul className="container-list">
          {containers?.map((c) => (
            <li key={c.service} className="container-row">
              <div className="container-main">
                <span className="service-name">{c.service}</span>
                <span className={badgeClass(c.state)}>{c.state}</span>
                {c.health && <span className={badgeClass(c.health)}>{c.health}</span>}
              </div>
              <div className="container-meta">
                <span className="muted">{c.image}</span>
                {(() => {
                  const m = metrics?.containers.find((x) => x.service === c.service);
                  return m ? <span className="muted">{fmtBytes(m.mem_bytes)}</span> : null;
                })()}
                {c.started_at && (
                  <span className="muted">since {new Date(c.started_at).toLocaleString()}</span>
                )}
              </div>
              <button type="button" className="danger small" onClick={() => restart(c.service)}>
                Restart
              </button>
            </li>
          ))}
        </ul>
      )}

      <LogViewer services={services} />
      <UpdateCard />
    </section>
  );
}

const LOG_TAIL = 200;

function LogViewer({ services }: { services: string[] }) {
  const [service, setService] = useState("");
  const [lines, setLines] = useState<string[]>([]);
  const [follow, setFollow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const logRef = useRef<HTMLPreElement>(null);

  // Selecting a service loads its tail; deselecting stops following.
  useEffect(() => {
    setLines([]);
    setError(null);
    if (service === "") {
      setFollow(false);
      return;
    }
    let cancelled = false;
    api
      .opsLogs(service, LOG_TAIL)
      .then((text) => {
        if (!cancelled) setLines(text.split("\n"));
      })
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [service]);

  useEffect(() => {
    if (!follow || service === "") return;
    const source = api.opsLogStream(service);
    source.onmessage = (event: MessageEvent<string>) => {
      setLines((prev) => [...prev, event.data]);
    };
    source.onerror = () => setError("Log stream disconnected.");
    return () => source.close();
  }, [follow, service]);

  // Auto-scroll so followed logs behave like `tail -f`.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run on every new line; the effect reads the DOM, not `lines`.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <div className="log-viewer">
      <h3>Logs</h3>
      <div className="log-controls">
        <label htmlFor="log-service">Service</label>
        <select id="log-service" value={service} onChange={(e) => setService(e.target.value)}>
          <option value="">— pick a service —</option>
          {services.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <label className="follow-toggle">
          <input
            type="checkbox"
            checked={follow}
            disabled={service === ""}
            onChange={(e) => setFollow(e.target.checked)}
          />
          Follow
        </label>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {service !== "" && (
        <pre className="log-output" ref={logRef} aria-label={`Logs for ${service}`}>
          {lines.join("\n")}
        </pre>
      )}
    </div>
  );
}
