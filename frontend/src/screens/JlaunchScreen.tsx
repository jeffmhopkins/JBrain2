// The Math launcher (jlaunch) — the self-contained full-screen overlay the "Math" tile
// opens (like JcodeScreen/Tasks). It lists the registered computations + past runs; picking
// one opens the tabbed job screen (Overview · Terminal · Result) with start/stop/kill, a live
// xterm, and — once a run finishes — a public results sharelink. Variant C from the GUI gate
// (docs/mocks/jlaunch/). Owner-only; the tile is hidden unless the launcher is enabled.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import { ChevronLeftIcon, ChevronRightIcon } from "../components/icons";
import { jlaunchShareUrl } from "../jlaunch/share";
import { attachTerminal, jlaunchTerminalWsUrl } from "../jlaunch/terminal";
import type { JlaunchJob, JlaunchShare, JlaunchSpec } from "../jlaunch/types";
import { useForeground } from "../visibility";

const LIVE = new Set(["queued", "cloning", "running"]);
const POLL_MS = 3000;

function isLive(job: JlaunchJob): boolean {
  return LIVE.has(job.state);
}

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; specs: JlaunchSpec[]; jobs: JlaunchJob[] }
  | { kind: "disabled" }
  | { kind: "error" };

export function JlaunchScreen({ onClose }: { onClose: () => void }) {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [openId, setOpenId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [specs, jobs] = await Promise.all([api.jlaunchSpecs(), api.jlaunchJobs()]);
      setState({ kind: "ready", specs, jobs });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setState({ kind: "disabled" });
      else setState({ kind: "error" });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (openId) {
    return (
      <JlaunchJobScreen
        jobId={openId}
        onClose={() => {
          setOpenId(null);
          void refresh();
        }}
      />
    );
  }

  const specs = state.kind === "ready" ? state.specs : [];
  const jobs = state.kind === "ready" ? state.jobs : [];

  async function start(spec: string) {
    try {
      const job = await api.jlaunchStart(spec);
      setOpenId(job.id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        alert("A job is already running — only one at a time. Open it to watch or stop it.");
      } else {
        alert("Couldn't start the job.");
      }
    }
  }

  return (
    <section className="jl-screen">
      <header className="jl-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back">
          <ChevronLeftIcon size={22} />
        </button>
        <h2 className="jl-bar-title">
          Math<span className="jl-dot">.</span>
        </h2>
      </header>

      {state.kind === "disabled" ? (
        <p className="jl-empty">The job launcher isn't enabled on this server.</p>
      ) : state.kind === "error" ? (
        <p className="jl-empty">Couldn't reach the job launcher — try again.</p>
      ) : state.kind === "loading" ? (
        <p className="jl-empty">Loading…</p>
      ) : (
        <div className="jl-body">
          <div className="jl-lab">Computations</div>
          {specs.map((spec) => (
            <button
              key={spec.name}
              type="button"
              className="jl-spec"
              onClick={() => void start(spec.name)}
            >
              <span className="jl-spec-main">
                <span className="jl-spec-title">{spec.title}</span>
                <span className="jl-spec-meta">
                  {spec.est_hours} · {spec.all_cores ? "all cores" : "single core"} · {spec.disk_gb}{" "}
                  GB · one-shot
                </span>
                <span className="jl-spec-note">{spec.notes}</span>
              </span>
              <span className="jl-spec-start">Start →</span>
            </button>
          ))}

          <div className="jl-lab jl-lab-mt">Runs</div>
          {jobs.length === 0 ? (
            <p className="jl-empty">No runs yet — start a computation above.</p>
          ) : (
            <div className="jl-rows">
              {jobs.map((job) => (
                <button
                  key={job.id}
                  type="button"
                  className="jl-row"
                  onClick={() => setOpenId(job.id)}
                >
                  <StatePill state={job.state} />
                  <span className="jl-row-main">
                    <span className="jl-row-title">{job.title}</span>
                    <span className="jl-row-sub">
                      {job.state === "running" || job.state === "cloning"
                        ? `${job.phase}…`
                        : relative(job.finished_at || job.started_at)}
                    </span>
                  </span>
                  <ChevronRightIcon size={16} />
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ---- job detail (tabbed) -------------------------------------------------------------

type Tab = "overview" | "terminal" | "result";

function JlaunchJobScreen({ jobId, onClose }: { jobId: string; onClose: () => void }) {
  const [job, setJob] = useState<JlaunchJob | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [busy, setBusy] = useState(false);
  const foreground = useForeground();

  const load = useCallback(async () => {
    try {
      setJob(await api.jlaunchGetJob(jobId));
    } catch {
      /* transient — the next poll retries */
    }
  }, [jobId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll while the job is live so state/phase/headline stay current without a manual refresh.
  useEffect(() => {
    if (!job || !isLive(job) || !foreground) return;
    const t = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(t);
  }, [job, foreground, load]);

  if (!job) {
    return (
      <section className="jl-screen">
        <JobBar title="…" state="" onClose={onClose} />
        <p className="jl-empty">Loading…</p>
      </section>
    );
  }

  const live = isLive(job);
  const id = job.id;

  async function control(kind: "stop" | "kill" | "delete") {
    setBusy(true);
    try {
      if (kind === "delete") {
        await api.jlaunchDelete(id);
        onClose();
        return;
      }
      const next = kind === "stop" ? await api.jlaunchStop(id) : await api.jlaunchKill(id);
      setJob(next);
    } catch {
      alert(`Couldn't ${kind} the job.`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="jl-screen">
      <JobBar title={job.spec_name} state={job.state} onClose={onClose} />

      <div className="jl-tabs" role="tablist" aria-label="Job">
        {(["overview", "terminal", "result"] as const).map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            className={`jl-tab${tab === t ? " on" : ""}`}
            onClick={() => setTab(t)}
          >
            {t === "overview" ? "Overview" : t === "terminal" ? "Terminal" : "Result"}
            {t === "terminal" && live && <span className="jl-tab-live" aria-hidden="true" />}
          </button>
        ))}
      </div>

      {tab === "overview" && <OverviewPane job={job} busy={busy} onControl={control} />}
      {tab === "terminal" && <TerminalPane job={job} />}
      {tab === "result" && <ResultPane job={job} />}
    </section>
  );
}

function JobBar({
  title,
  state,
  onClose,
}: {
  title: string;
  state: string;
  onClose: () => void;
}): ReactNode {
  return (
    <header className="jl-bar">
      <button type="button" className="icon-btn" onClick={onClose} aria-label="Back">
        <ChevronLeftIcon size={22} />
      </button>
      <h2 className="jl-bar-title jl-mono">{title}</h2>
      {state && <StatePill state={state} />}
    </header>
  );
}

function OverviewPane({
  job,
  busy,
  onControl,
}: {
  job: JlaunchJob;
  busy: boolean;
  onControl: (k: "stop" | "kill" | "delete") => void;
}): ReactNode {
  const live = isLive(job);
  const times = new Map(job.phase_times.map((p) => [p.phase, p.seconds]));
  return (
    <div className="jl-body">
      <div className="jl-controls">
        {live ? (
          <>
            <button
              type="button"
              className="jl-btn stop"
              disabled={busy}
              onClick={() => onControl("stop")}
            >
              Stop
            </button>
            <button
              type="button"
              className="jl-btn kill"
              disabled={busy}
              onClick={() => onControl("kill")}
            >
              Kill
            </button>
          </>
        ) : (
          <button
            type="button"
            className="jl-btn danger"
            disabled={busy}
            onClick={() => {
              if (
                confirm("Delete this run? Its checkout, ~10 GB scratch, and any sharelink go too.")
              )
                onControl("delete");
            }}
          >
            Delete run
          </button>
        )}
      </div>

      <div className="jl-card">
        <h3 className="jl-h3">Job</h3>
        <div className="jl-meta">
          <div className="jl-meta-title">{job.title}</div>
          <div>repo · {job.repo}</div>
          <div>
            state · {job.state}
            {job.error ? ` — ${job.error}` : ""}
          </div>
          {job.started_at && <div>started · {relative(job.started_at)}</div>}
        </div>
      </div>

      <div className="jl-card">
        <h3 className="jl-h3">Phases</h3>
        <div className="jl-phases">
          {phasesFor(job).map((name) => {
            const done = times.has(name);
            const now = live && job.phase === name;
            const secs = times.get(name);
            return (
              <div key={name} className={`jl-phase${done ? " done" : now ? " now" : ""}`}>
                <span className="jl-phase-name">{name}</span>
                <span className="jl-phase-t">{secs != null ? fmtSecs(secs) : now ? "…" : "—"}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function TerminalPane({ job }: { job: JlaunchJob }): ReactNode {
  if (isLive(job)) return <LiveTerminal jobId={job.id} />;
  // Finished: no PTY to attach to — show the retained console tail.
  return (
    <div className="jl-body">
      <pre className="jl-console">{job.console_tail || "(no console output retained)"}</pre>
    </div>
  );
}

function LiveTerminal({ jobId }: { jobId: string }): ReactNode {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let disposed = false;
    let cleanup = () => {};
    void (async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([
        import("@xterm/xterm"),
        import("@xterm/addon-fit"),
      ]);
      if (disposed || !host.current) return;
      const term = new Terminal({
        fontSize: 9,
        fontFamily: "ui-monospace, Menlo, Consolas, monospace",
        cursorBlink: false,
        disableStdin: true, // a one-shot job's console is read-only
        theme: { background: "#0b0c0e", foreground: "#c6c8cb" },
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(el);
      fit.fit();
      const ws = new WebSocket(jlaunchTerminalWsUrl(jobId));
      const h = attachTerminal(term, ws);
      ws.onclose = () => {
        if (!disposed) term.write("\r\n\x1b[2m— run ended —\x1b[0m\r\n");
      };
      const ro = new ResizeObserver(() => fit.fit());
      ro.observe(el);
      cleanup = () => {
        ro.disconnect();
        h.detach();
        ws.close();
        term.dispose();
      };
    })();
    return () => {
      disposed = true;
      cleanup();
    };
  }, [jobId]);
  return (
    <div className="jl-body">
      <div className="jl-term" ref={host} />
    </div>
  );
}

function ResultPane({ job }: { job: JlaunchJob }): ReactNode {
  const [shares, setShares] = useState<JlaunchShare[]>([]);
  const [link, setLink] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadShares = useCallback(async () => {
    try {
      setShares(await api.jlaunchListShares(job.id));
    } catch {
      setShares([]);
    }
  }, [job.id]);

  useEffect(() => {
    if (job.state === "succeeded") void loadShares();
  }, [job.state, loadShares]);

  if (job.state !== "succeeded") {
    return (
      <div className="jl-body">
        <p className="jl-empty">
          The headline + sharelink appear here once the run finishes with VERIFICATION OK.
        </p>
      </div>
    );
  }

  async function mint() {
    setBusy(true);
    try {
      const token = await api.jlaunchMintShare(job.id);
      setLink(jlaunchShareUrl(token.token));
      await loadShares();
    } catch {
      alert("Couldn't generate the sharelink.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="jl-body">
      <div className="jl-card">
        <h3 className="jl-h3">Headline{job.verify_ok ? " · verified OK" : ""}</h3>
        <pre className="jl-headline">{job.headline.join("\n")}</pre>
        <div className="jl-specs">
          {machineChips(job.machine).map((c) => (
            <span key={c} className="jl-chip">
              {c}
            </span>
          ))}
          {job.artifact_bytes != null && (
            <span className="jl-chip">{fmtBytes(job.artifact_bytes)}</span>
          )}
        </div>
      </div>

      <div className="jl-card">
        <h3 className="jl-h3">Share</h3>
        <button type="button" className="jl-btn share" disabled={busy} onClick={() => void mint()}>
          {busy ? "Generating…" : "Generate public sharelink →"}
        </button>
        {link && <div className="jl-link">{link}</div>}
        {shares.length > 0 && (
          <div className="jl-sharelist">
            {shares.map((s) => (
              <div key={s.id} className="jl-shareitem">
                <span>
                  {s.label || "shared results"} · {s.view_count} view
                  {s.view_count === 1 ? "" : "s"}
                </span>
                <button
                  type="button"
                  className="jl-revoke"
                  onClick={async () => {
                    await api.jlaunchRevokeShare(job.id, s.id);
                    await loadShares();
                  }}
                >
                  Revoke
                </button>
              </div>
            ))}
          </div>
        )}
        <p className="jl-sharenote">
          No login · results page + artifact download · revoke anytime.
        </p>
      </div>
    </div>
  );
}

// ---- helpers -------------------------------------------------------------------------

function StatePill({ state }: { state: string }): ReactNode {
  const cls =
    state === "succeeded"
      ? "ok"
      : state === "failed"
        ? "fail"
        : state === "killed"
          ? "kill"
          : "run";
  return <span className={`jl-pill ${cls}`}>{state || "…"}</span>;
}

function phasesFor(job: JlaunchJob): string[] {
  const names = job.phase_times.map((p) => p.phase);
  // Ensure the current live phase shows even before it has a recorded time.
  if (job.phase && !names.includes(job.phase)) names.push(job.phase);
  return names.length ? names : ["clone", "install", "smoke", "run"];
}

function machineChips(machine: Record<string, unknown>): string[] {
  const out: string[] = [];
  if (typeof machine.cpu === "string") out.push(machine.cpu);
  if (typeof machine.cores === "number") out.push(`${machine.cores} cores`);
  if (typeof machine.mem_bytes === "number") out.push(`${fmtBytes(machine.mem_bytes)} RAM`);
  return out;
}

function fmtSecs(s: number): string {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function fmtBytes(b: number): string {
  if (b >= 1024 ** 3) return `${(b / 1024 ** 3).toFixed(1)} GB`;
  if (b >= 1024 ** 2) return `${Math.round(b / 1024 ** 2)} MB`;
  return `${Math.round(b / 1024)} KB`;
}

function relative(iso: string | null): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.floor(ms / 60000);
  if (min < 1) return "now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
