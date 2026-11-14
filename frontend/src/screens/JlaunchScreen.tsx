// The Jobs launcher (jlaunch) — the self-contained full-screen overlay the "Jobs" tile
// opens (like JcodeScreen/Tasks). Two tabs: Jobs (the registered computations as compact
// rows that expand to their notes and a Start button) and Runs (past and current runs, a
// finished one deletable in place). Picking a run opens the tabbed job screen (Overview · Terminal · Result) with start/stop/kill, a live
// xterm, a Live card for a job that keeps a live folder, and — once a run finishes — a
// download and a public results sharelink. Variant C from the GUI gate (docs/mocks/jlaunch/);
// the Live card is variant A of docs/mocks/jlaunch-live/. Owner-only; the tile is hidden
// unless the launcher is enabled.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import { EngineBanner } from "../components/EngineBanner";
import { ChevronDownIcon, ChevronLeftIcon, ChevronRightIcon, TrashIcon } from "../components/icons";
import { fmtEta, jlaunchLiveUrl, jlaunchResultUrl } from "../jlaunch/live";
import { jlaunchShareUrl } from "../jlaunch/share";
import { attachTerminal, jlaunchTerminalWsUrl } from "../jlaunch/terminal";
import type {
  JlaunchJob,
  JlaunchLiveFile,
  JlaunchProgress,
  JlaunchShare,
  JlaunchSpec,
} from "../jlaunch/types";
import { useForeground } from "../visibility";

const LIVE = new Set(["queued", "cloning", "running"]);
const POLL_MS = 3000;
// The live folder changes once per training batch at most; no need to chase it faster.
const LIVE_POLL_MS = 10000;

function isLive(job: JlaunchJob): boolean {
  return LIVE.has(job.state);
}

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; specs: JlaunchSpec[]; jobs: JlaunchJob[] }
  | { kind: "disabled" }
  | { kind: "error" };

type ListTab = "jobs" | "runs";

export function JlaunchScreen({ onClose }: { onClose: () => void }) {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [openId, setOpenId] = useState<string | null>(null);
  const [tab, setTab] = useState<ListTab>("jobs");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  // The one job the Runs tab is narrowed to (from a job's View runs), and the groups the
  // owner has folded shut ("c:<category>" on Jobs, "r:<spec>" on Runs).
  const [focus, setFocus] = useState<string | null>(null);
  const [shut, setShut] = useState<Set<string>>(() => new Set());
  const foreground = useForeground();

  // Only the newest refresh may land: a poll that started before a delete must not put
  // the deleted row back when it finishes after the delete's own refresh.
  const seq = useRef(0);
  const refresh = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const [specs, jobs] = await Promise.all([api.jlaunchSpecs(), api.jlaunchJobs()]);
      if (mine === seq.current) setState({ kind: "ready", specs, jobs });
    } catch (err) {
      if (mine !== seq.current) return;
      if (err instanceof ApiError && err.status === 404) setState({ kind: "disabled" });
      // A blip during the live poll keeps the last good list; the next poll retries.
      else setState((prev) => (prev.kind === "ready" ? prev : { kind: "error" }));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const specs = state.kind === "ready" ? state.specs : [];
  const jobs = state.kind === "ready" ? state.jobs : [];
  const anyLive = jobs.some(isLive);

  // Keep a running job's phase and pill current on the Runs list, as the job screen does.
  useEffect(() => {
    if (openId || !anyLive || !foreground) return;
    const t = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(t);
  }, [openId, anyLive, foreground, refresh]);

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

  function toggle(key: string) {
    setShut((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

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

  async function remove(job: JlaunchJob) {
    if (
      !confirm(
        `Delete ${runLabel(job)} (${job.state}) of “${job.title}”? Its files and any sharelink go too.`,
      )
    )
      return;
    setDeleting(job.id);
    try {
      await api.jlaunchDelete(job.id);
    } catch (err) {
      alert(
        err instanceof ApiError && err.status === 409
          ? "That run is still going — stop it first."
          : "Couldn't delete the run.",
      );
    } finally {
      setDeleting(null);
      void refresh();
    }
  }

  return (
    <section className="jl-screen">
      <header className="jl-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back">
          <ChevronLeftIcon size={22} />
        </button>
        <h2 className="jl-bar-title">
          Jobs<span className="jl-dot">.</span>
        </h2>
      </header>
      <EngineBanner />

      {state.kind === "disabled" ? (
        <p className="jl-empty">The job launcher isn't enabled on this server.</p>
      ) : state.kind === "error" ? (
        <p className="jl-empty">Couldn't reach the job launcher — try again.</p>
      ) : state.kind === "loading" ? (
        <p className="jl-empty">Loading…</p>
      ) : (
        <div className="jl-body">
          <div className="jl-tabs" role="tablist" aria-label="Jobs and runs">
            {(["jobs", "runs"] as const).map((t) => (
              <button
                key={t}
                type="button"
                role="tab"
                aria-selected={tab === t}
                className={`jl-tab${tab === t ? " on" : ""}`}
                onClick={() => {
                  setTab(t);
                  setFocus(null);
                }}
              >
                {t === "jobs" ? "Jobs" : "Runs"}
                <span className="jl-tab-n">{t === "jobs" ? specs.length : jobs.length}</span>
                {t === "runs" && anyLive && <span className="jl-tab-live" aria-hidden="true" />}
              </button>
            ))}
          </div>

          {tab === "jobs" ? (
            groupBy(specs, (spec) => spec.category || "Other").map(([category, group]) => (
              <Group
                key={category}
                label={category}
                count={group.length}
                open={!shut.has(`c:${category}`)}
                onToggle={() => toggle(`c:${category}`)}
              >
                <div className="jl-rows">
                  {group.map((spec) => {
                    const open = expanded === spec.name;
                    const runs = jobs.filter((j) => j.spec_name === spec.name).length;
                    return (
                      <div key={spec.name} className={`jl-spec${open ? " open" : ""}`}>
                        <button
                          type="button"
                          className="jl-spec-head"
                          aria-expanded={open}
                          onClick={() => setExpanded(open ? null : spec.name)}
                        >
                          <span className="jl-row-main">
                            <span className="jl-spec-title">{spec.title}</span>
                            <span className="jl-row-sub">
                              {spec.est_hours} · {spec.all_cores ? "all cores" : "single core"} ·{" "}
                              {spec.disk_gb} GB
                            </span>
                          </span>
                          <span className="jl-spec-chev" aria-hidden="true">
                            <ChevronDownIcon size={16} />
                          </span>
                        </button>
                        {open && (
                          // Only the Start button starts a run: a stray tap while scrolling
                          // must not launch hours of work (owner, 2026-11-14).
                          <div className="jl-spec-detail">
                            <p className="jl-spec-note">{spec.notes}</p>
                            <div className="jl-spec-actions">
                              <button
                                type="button"
                                className="jl-spec-start"
                                onClick={() => void start(spec.name)}
                                aria-label={`Start ${spec.title}`}
                              >
                                Start run →
                              </button>
                              <button
                                type="button"
                                className="jl-spec-runs"
                                disabled={runs === 0}
                                onClick={() => {
                                  setFocus(spec.name);
                                  setTab("runs");
                                }}
                                aria-label={`View runs of ${spec.title}`}
                              >
                                {runs === 0 ? "No runs yet" : `View runs (${runs})`}
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </Group>
            ))
          ) : jobs.length === 0 ? (
            <p className="jl-empty">No runs yet — start one from the Jobs tab.</p>
          ) : (
            <>
              {focus && (
                <button type="button" className="jl-filter" onClick={() => setFocus(null)}>
                  Showing one job's runs · <span className="jl-filter-x">Show all</span>
                </button>
              )}
              {runGroups(jobs, specs)
                .filter((g) => !focus || g.spec === focus)
                .map((g) => (
                  <Group
                    key={g.spec}
                    label={g.title}
                    count={g.runs.length}
                    live={g.runs.some(isLive)}
                    open={focus === g.spec || !shut.has(`r:${g.spec}`)}
                    onToggle={() => toggle(`r:${g.spec}`)}
                  >
                    <div className="jl-rows">
                      {g.runs.map((job) => (
                        <div key={job.id} className="jl-run">
                          <button
                            type="button"
                            className="jl-row"
                            onClick={() => setOpenId(job.id)}
                            aria-label={`Open ${runLabel(job)} of ${job.title}`}
                          >
                            <StatePill state={job.state} />
                            <span className="jl-row-main">
                              <span className="jl-row-title">
                                <span className="jl-rev">{runLabel(job)}</span>
                                {job.note ? (
                                  <span className="jl-run-note"> · {job.note}</span>
                                ) : null}
                              </span>
                              <span className="jl-row-sub">
                                {job.state === "running" || job.state === "cloning"
                                  ? `${job.phase}…`
                                  : relative(
                                      job.finished_at || job.started_at || job.created_at || null,
                                    )}
                              </span>
                            </span>
                            <ChevronRightIcon size={16} />
                          </button>
                          {/* A live run has no delete here: stop it from its screen first. The
                              placeholder keeps every row's chevron in one column. */}
                          {isLive(job) ? (
                            <span className="jl-run-del none" aria-hidden="true">
                              <TrashIcon size={16} />
                            </span>
                          ) : (
                            <button
                              type="button"
                              className="jl-run-del"
                              disabled={deleting === job.id}
                              onClick={() => void remove(job)}
                              aria-label={`Delete ${runLabel(job)} of ${job.title}`}
                            >
                              <TrashIcon size={16} />
                            </button>
                          )}
                        </div>
                      ))}
                    </div>
                  </Group>
                ))}
            </>
          )}
        </div>
      )}
    </section>
  );
}

/** A collapsible section of the Jobs or Runs tab: a category of jobs, or one job's runs. */
function Group({
  label,
  count,
  live = false,
  open,
  onToggle,
  children,
}: {
  label: string;
  count: number;
  live?: boolean;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}): ReactNode {
  return (
    <section className={`jl-group${open ? " open" : ""}`}>
      <button type="button" className="jl-group-head" aria-expanded={open} onClick={onToggle}>
        <span className="jl-group-label">{label}</span>
        <span className="jl-group-n">{count}</span>
        {live && <span className="jl-tab-live" aria-hidden="true" />}
        <span className="jl-spec-chev" aria-hidden="true">
          <ChevronDownIcon size={14} />
        </span>
      </button>
      {open && children}
    </section>
  );
}

function groupBy<T>(items: T[], key: (item: T) => string): [string, T[]][] {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const k = key(item);
    groups.set(k, [...(groups.get(k) ?? []), item]);
  }
  return [...groups];
}

/** Runs grouped under their job, the job with the newest run first (the api lists runs
 * newest first, so first-seen order is that). A job title comes from its spec when the
 * launcher still has it, else from the run. */
function runGroups(
  jobs: JlaunchJob[],
  specs: JlaunchSpec[],
): { spec: string; title: string; runs: JlaunchJob[] }[] {
  const titles = new Map(specs.map((s) => [s.name, s.title]));
  return groupBy(jobs, (j) => j.spec_name).map(([spec, runs]) => ({
    spec,
    title: titles.get(spec) ?? runs[0]?.title ?? spec,
    runs,
  }));
}

/** "v3", or "run" for a row from before runs were numbered. */
function runLabel(job: JlaunchJob): string {
  return job.revision ? `v${job.revision}` : "run";
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
      <JobBar
        title={job.revision ? `${job.spec_name} · v${job.revision}` : job.spec_name}
        state={job.state}
        onClose={onClose}
      />

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

      {tab === "overview" && (
        <OverviewPane job={job} busy={busy} onControl={control} onNoted={setJob} />
      )}
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
  onNoted,
}: {
  job: JlaunchJob;
  busy: boolean;
  onControl: (k: "stop" | "kill" | "delete") => void;
  onNoted: (job: JlaunchJob) => void;
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

      <LiveCard job={job} />

      <div className="jl-card">
        <h3 className="jl-h3">Job</h3>
        <div className="jl-meta">
          <div className="jl-meta-title">{job.title}</div>
          <div>repo · {job.repo}</div>
          <div>
            state · {job.state}
            {job.error ? ` — ${job.error}` : ""}
          </div>
          {(job.started_at || job.created_at) && (
            <div>started · {relative(job.started_at || job.created_at || null)}</div>
          )}
        </div>
        <NoteField job={job} onSaved={onNoted} />
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

/** The owner's note on a run — what it was for — so v3 and v4 of one job read apart. */
function NoteField({
  job,
  onSaved,
}: {
  job: JlaunchJob;
  onSaved: (job: JlaunchJob) => void;
}): ReactNode {
  const saved = job.note ?? "";
  const [draft, setDraft] = useState(saved);
  const [saving, setSaving] = useState(false);
  // A newly saved note (this save, or one from a debug token the job poll brings back)
  // becomes the draft.
  useEffect(() => setDraft(saved), [saved]);

  async function save() {
    setSaving(true);
    try {
      onSaved({ ...job, ...(await api.jlaunchSetNote(job.id, draft)) });
    } catch {
      alert("Couldn't save the note.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      className="jl-note"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <input
        className="jl-note-input"
        value={draft}
        maxLength={280}
        placeholder="Note — what was this run for?"
        aria-label="Run note"
        onChange={(e) => setDraft(e.target.value)}
      />
      {draft.trim() !== saved && (
        <button type="submit" className="jl-btn" disabled={saving}>
          Save
        </button>
      )}
    </form>
  );
}

/** A running job's live folder: how far along it is, how it's doing, and its files (a
 * replay page opens in a new tab; the rest download). Hidden when the job keeps none. */
export function LiveCard({ job }: { job: JlaunchJob }): ReactNode {
  const [files, setFiles] = useState<JlaunchLiveFile[] | null>(null);
  const [progress, setProgress] = useState<JlaunchProgress | null>(null);
  // A 404 means this job's spec keeps no live folder: stop asking for the rest of the run.
  const [none, setNone] = useState(false);
  const foreground = useForeground();
  const live = isLive(job);

  const load = useCallback(async () => {
    let listed: JlaunchLiveFile[];
    try {
      listed = await api.jlaunchLiveFiles(job.id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setNone(true);
        setFiles(null);
      }
      return; // anything else is transient: keep what is showing, the next poll retries
    }
    setFiles(listed);
    if (!listed.some((f) => f.name === "progress.json")) {
      setProgress(null);
      return;
    }
    try {
      setProgress(await api.jlaunchProgress(job.id));
    } catch {
      /* mid-rewrite or malformed — keep the last good one */
    }
  }, [job.id]);

  // Once more when the job settles, so the card flips to the final files.
  // biome-ignore lint/correctness/useExhaustiveDependencies: job.state is the trigger
  useEffect(() => {
    void load();
  }, [load, job.state]);

  useEffect(() => {
    if (!live || !foreground || none) return;
    const t = setInterval(() => void load(), LIVE_POLL_MS);
    return () => clearInterval(t);
  }, [live, foreground, none, load]);

  if (!files) return null;
  const pct = typeof progress?.percent === "number" ? progress.percent : null;
  const rows = summaryRows(progress);
  const pages = files.filter((f) => f.name.endsWith(".html"));
  const others = files.filter((f) => !f.name.endsWith(".html") && f.name !== "progress.json");
  return (
    <div className="jl-card">
      <h3 className="jl-h3">
        Live{progress?.title ? ` · ${progress.title}` : ""}
        {live && <span className="jl-tab-live" aria-hidden="true" />}
      </h3>
      {pct != null && (
        <>
          <div className="jl-progress" aria-hidden="true">
            <div className="jl-progress-fill" style={{ width: `${Math.min(100, pct)}%` }} />
          </div>
          <div className="jl-progress-meta">
            <span>{pct.toFixed(0)}%</span>
            <span>
              {live && typeof progress?.eta_s === "number"
                ? fmtEta(progress.eta_s)
                : progress?.updated_at
                  ? `updated ${relative(progress.updated_at)}`
                  : ""}
            </span>
          </div>
        </>
      )}
      {rows.length > 0 && (
        <div className="jl-times">
          {rows.map((row, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: labels may repeat; the order is the file's
            <div key={i} className="jl-time">
              <span>{row.label}</span>
              <span>{row.value}</span>
            </div>
          ))}
        </div>
      )}
      {files.length === 0 && (
        <p className="jl-sharenote">Nothing written yet. It fills in as the run gets going.</p>
      )}
      {pages.map((f) => (
        <a
          key={f.name}
          className="jl-btn share jl-linkbtn"
          href={jlaunchLiveUrl(job.id, f.name)}
          target="_blank"
          rel="noopener noreferrer"
        >
          {f.name !== "replay.html"
            ? `Open ${f.name}`
            : live
              ? "Watch it train in 3D (live)"
              : "Watch the final walk in 3D"}{" "}
          ↗
        </a>
      ))}
      {others.length > 0 && (
        <div className="jl-sharelist">
          {others.map((f) => (
            <div key={f.name} className="jl-shareitem">
              <span className="jl-file">
                {f.name} · {fmtBytes(f.bytes)}
              </span>
              <a className="jl-dl" href={jlaunchLiveUrl(job.id, f.name, true)} download>
                Download
              </a>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** progress.json's summary rows, kept only where they are plain text: the job wrote the
 * file, and a wrong shape must not take the job screen down with it. */
export function summaryRows(progress: JlaunchProgress | null): { label: string; value: string }[] {
  const raw: unknown = progress?.summary;
  if (!Array.isArray(raw)) return [];
  const plain = (v: unknown) => typeof v === "string" || typeof v === "number";
  return raw
    .filter((r): r is { label: string | number; value: string | number } => {
      const row = r as { label?: unknown; value?: unknown } | null;
      return !!row && plain(row.label) && plain(row.value);
    })
    .map((r) => ({ label: String(r.label), value: String(r.value) }));
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
          The headline, download and sharelink appear here once the run finishes and passes
          verification.
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
        <h3 className="jl-h3">Download</h3>
        <a className="jl-btn share jl-linkbtn" href={jlaunchResultUrl(job.id)} download>
          Download {job.artifact_name}
          {job.artifact_bytes != null ? ` · ${fmtBytes(job.artifact_bytes)}` : ""}
        </a>
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
