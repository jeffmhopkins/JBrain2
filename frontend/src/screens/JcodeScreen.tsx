// Code mode (jcode) — the resume-first launcher (docs/DESIGN.md "jcode", variant B).
// The session list is the hero (Chats-picker paradigm); "New session" opens setup in
// a bottom sheet. Tapping a row opens the tabbed session screen, which this screen
// stacks over itself (its own back returns to the list). A self-contained full-screen
// overlay like Tasks/Automations.

import { type ReactNode, type TouchEvent, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import { Sheet } from "../components/Sheet";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  PencilIcon,
  PlusIcon,
  TrashIcon,
} from "../components/icons";
import type {
  ExternalSession,
  JcodeModelStatus,
  JcodePowerStatus,
  JcodeSession,
  NewSessionInput,
} from "../jcode/types";
import { type Drag, RAIL_WIDTH, beginDrag, endDrag, moveDrag } from "../notes/swipe";
import { ExternalSessionScreen } from "./ExternalSessionScreen";
import { JcodeSessionScreen } from "./JcodeSessionScreen";

// An open external endpoint: the session row plus, right after minting, its one-time
// secret and the URL the remote points at (reconstructed from the origin for existing
// ones, since the list carries no secret/url).
type OpenExternal = { session: ExternalSession; secret: string | null; url: string };

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; sessions: JcodeSession[] }
  | { kind: "disabled" } // 404 — code mode isn't enabled on this server
  | { kind: "error" };

function isToday(iso: string): boolean {
  const d = new Date(iso);
  const n = new Date();
  return (
    d.getFullYear() === n.getFullYear() &&
    d.getMonth() === n.getMonth() &&
    d.getDate() === n.getDate()
  );
}

function relative(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.floor(ms / 60000);
  if (min < 1) return "now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

type Bucket = "today" | "older" | "archived";

export function JcodeScreen({ onClose }: { onClose: () => void }) {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [bucket, setBucket] = useState<Bucket>("today");
  const [newOpen, setNewOpen] = useState(false);
  const [open, setOpen] = useState<JcodeSession | null>(null);
  const [openExt, setOpenExt] = useState<OpenExternal | null>(null);
  const [external, setExternal] = useState<ExternalSession[]>([]);
  // One swipe rail open at a time (like the agent-sessions manager).
  const [railId, setRailId] = useState<string | null>(null);
  // The master power switch state, and which transition (if any) the modal is running.
  const [power, setPower] = useState<JcodePowerStatus | null>(null);
  const [powerAction, setPowerAction] = useState<"on" | "off" | null>(null);

  async function refresh() {
    // The power state gates everything else: while off the services are down, so the
    // session list can't load — the switch (not an error) is what the owner needs.
    try {
      setPower(await api.jcodePower());
    } catch {
      setPower(null);
    }
    try {
      setState({ kind: "ready", sessions: await api.jcodeSessions() });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setState({ kind: "disabled" });
      else setState({ kind: "error" });
    }
    // External endpoints live alongside coding sessions; a failure here just leaves the
    // section empty (it never blocks the main list).
    try {
      setExternal(await api.externalSessions());
    } catch {
      setExternal([]);
    }
  }

  function endpointUrl(id: string): string {
    return `${window.location.origin}/api/ext/llm/${id}`;
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: load the index once on mount; refresh is also invoked on session-close.
  useEffect(() => {
    void refresh();
  }, []);

  // The open session stacks over the list; its back returns here and refreshes
  // the index (status/last-active may have moved).
  if (open) {
    return (
      <JcodeSessionScreen
        session={open}
        onClose={() => {
          setOpen(null);
          void refresh();
        }}
      />
    );
  }

  if (openExt) {
    return (
      <ExternalSessionScreen
        session={openExt.session}
        secret={openExt.secret}
        url={openExt.url}
        onChanged={() => void refresh()}
        onClose={() => {
          setOpenExt(null);
          void refresh();
        }}
      />
    );
  }

  // Archived sessions live in their own bucket; the live list splits by last-active.
  const sessions = state.kind === "ready" ? state.sessions : [];
  const live = sessions.filter((s) => !s.archived);
  const today = live.filter((s) => isToday(s.last_active_at));
  const older = live.filter((s) => !isToday(s.last_active_at));
  const archived = sessions.filter((s) => s.archived);
  const counts: Record<Bucket, number> = {
    today: today.length,
    older: older.length,
    archived: archived.length,
  };
  const rows = bucket === "today" ? today : bucket === "older" ? older : archived;

  async function start(input: NewSessionInput) {
    const session = await api.jcodeCreateSession(input);
    setNewOpen(false);
    setOpen(session);
  }

  // Open a session from the list. A paused (stopped) session is restarted first so it
  // opens live — "restart from the session manager". If the restart fails we open it
  // STILL stopped, so the session screen shows its own Restart prompt rather than mounting
  // a terminal against a sandbox that's still down.
  async function openSession(session: JcodeSession) {
    if (session.status === "stopped") {
      try {
        await api.jcodeRestartSession(session.id);
        setOpen({ ...session, status: "ready" });
      } catch {
        setOpen(session); // still stopped → the screen offers Restart
      }
      return;
    }
    setOpen(session);
  }

  // External endpoint: mint, then open its screen with the one-time secret + URL.
  async function startExternal(label: string) {
    const m = await api.externalMint(label);
    setNewOpen(false);
    setOpenExt({
      session: {
        id: m.id,
        label: m.label,
        enabled: true,
        created_at: new Date().toISOString(),
        expires_at: m.expires_at,
        last_used_at: null,
        in_tokens: 0,
        out_tokens: 0,
        requests: 0,
      },
      secret: m.token,
      url: m.url,
    });
  }

  // Rail actions optimistically refresh the index so the row moves/leaves at once.
  async function rename(id: string, title: string) {
    await api.jcodeRenameSession(id, title);
    await refresh();
  }
  async function archive(id: string) {
    await api.jcodeArchiveSession(id);
    await refresh();
  }
  async function unarchive(id: string) {
    await api.jcodeUnarchiveSession(id);
    await refresh();
  }
  async function remove(id: string) {
    await api.jcodeDeleteSession(id);
    await refresh();
  }

  return (
    <section className="jcode-screen">
      <header className="jcode-bar">
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Back">
          <ChevronLeftIcon size={22} />
        </button>
        <h2 className="jcode-bar-title">
          jcode<span className="jcode-dot">.</span>
        </h2>
        {/* Master power switch — brings the on-box coder + jcode-only services up/down.
            Only shown once the services are provisioned (there's something to toggle). */}
        {power?.provisioned && (
          <button
            type="button"
            role="switch"
            aria-checked={power.on}
            aria-label="Code mode power"
            className="jcode-power"
            disabled={powerAction !== null}
            onClick={() => setPowerAction(power.on ? "off" : "on")}
          >
            <span className="jcode-power-label">{power.on ? "On" : "Off"}</span>
            <span className={`jcode-power-track${power.on ? " on" : ""}`}>
              <span className="jcode-power-knob" />
            </span>
          </button>
        )}
      </header>

      {state.kind === "disabled" ? (
        <p className="jcode-empty">Code mode isn't enabled on this server.</p>
      ) : power?.provisioned && !power.on ? (
        <PowerOffPanel onPowerOn={() => setPowerAction("on")} />
      ) : state.kind === "error" ? (
        <p className="jcode-empty">Couldn't reach code mode — try again.</p>
      ) : (
        <div className="jcode-body">
          <button type="button" className="jcode-newbtn" onClick={() => setNewOpen(true)}>
            <span className="jcode-newplus">
              <PlusIcon size={18} />
            </span>
            <span>
              <span className="jcode-newtitle">New session</span>
              <span className="jcode-newsub">clone a repo into a fresh sandbox</span>
            </span>
          </button>

          {/* Today · Older · Archived — swipe a row left for rename/archive/delete,
              mirroring the agent-sessions manager. */}
          <div className="jcode-buckets" role="tablist" aria-label="Sessions">
            {(["today", "older", "archived"] as const).map((b) => (
              <button
                key={b}
                type="button"
                role="tab"
                aria-selected={bucket === b}
                className={`jcode-bk${bucket === b ? " on" : ""}`}
                onClick={() => setBucket(b)}
              >
                {b === "today" ? "Today" : b === "older" ? "Older" : "Archived"}
                <span className="jcode-bkpill">{counts[b]}</span>
              </button>
            ))}
          </div>

          {state.kind === "loading" ? (
            <p className="jcode-empty">Loading…</p>
          ) : rows.length === 0 ? (
            <p className="jcode-empty">
              {bucket === "archived"
                ? "No archived sessions."
                : `No ${bucket} sessions — tap New session to start.`}
            </p>
          ) : (
            <div className="jcode-rows">
              {rows.map((s) => (
                <JcodeSessionRow
                  key={s.id}
                  session={s}
                  onOpen={openSession}
                  onRename={rename}
                  onArchive={archive}
                  onUnarchive={unarchive}
                  onDelete={remove}
                  railOpen={railId === s.id}
                  onRailChange={(o) => setRailId(o ? s.id : null)}
                />
              ))}
            </div>
          )}

          {/* External LLM endpoints — token-gated public access to the on-box coder.
              Listed here (not in the Today/Older buckets) since they're not coding
              sessions; tap to see usage + the on/off toggle. */}
          {external.length > 0 && (
            <div className="jcode-extlist">
              <div className="jcode-extlist-head">External LLM endpoints</div>
              {external.map((e) => (
                <button
                  type="button"
                  key={e.id}
                  className="jcode-row"
                  onClick={() => setOpenExt({ session: e, secret: null, url: endpointUrl(e.id) })}
                >
                  <span
                    className={`jcode-sd${e.enabled ? " live" : ""}`}
                    title={e.enabled ? "on" : "off"}
                  />
                  <span className="jcode-main">
                    <span className="jcode-repo">{e.label || "external session"}</span>
                    <span className="jcode-sub">
                      {e.enabled ? "on" : "off"} · {e.requests.toLocaleString()} req ·{" "}
                      {(e.in_tokens + e.out_tokens).toLocaleString()} tok
                    </span>
                  </span>
                  <ChevronRightIcon size={16} />
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {newOpen && (
        <NewSessionSheet
          onClose={() => setNewOpen(false)}
          onStart={start}
          onStartExternal={startExternal}
        />
      )}

      {powerAction && power && (
        <JcodePowerModal
          action={powerAction}
          initial={power}
          onClose={(latest) => {
            setPowerAction(null);
            if (latest) setPower(latest);
            void refresh();
          }}
        />
      )}
    </section>
  );
}

// The powered-off body: the services are down, so the session list can't load. Explains
// the state and offers the same bring-up the header switch runs.
function PowerOffPanel({ onPowerOn }: { onPowerOn: () => void }): ReactNode {
  return (
    <div className="jcode-body">
      <div className="jcode-poweroff">
        <h3>Code mode is off</h3>
        <p>
          The coder is unloaded and the jcode-only services are stopped, freeing the box for
          everything else. Switch it on to bring the services up and load the coder.
        </p>
        <button type="button" className="jcode-act teal" onClick={onPowerOn}>
          Power on
        </button>
      </div>
    </div>
  );
}

// Rough cold-load read rate (s/GB) for the model bar's FALLBACK estimate when the gateway
// reports no real fraction — mirrors the session screen's loading bar.
const POWER_LOAD_SEC_PER_GB = 1.2;

// The bring-up / shut-down modal. Powering ON runs a two-step sequence with progress:
// start the services, then warm the coder onto the box (reusing /jcode/model/warm + the
// load bar). Powering OFF confirms first when sessions are live (stopping the services
// halts their shells), then stops everything. `onClose` hands back the latest power state.
function JcodePowerModal({
  action,
  initial,
  onClose,
}: {
  action: "on" | "off";
  initial: JcodePowerStatus;
  onClose: (latest: JcodePowerStatus | null) => void;
}): ReactNode {
  const powerOn = action === "on";
  // Powering off with live sessions holds at a confirm gate before halting anything.
  const [confirmed, setConfirmed] = useState(powerOn || initial.live_sessions === 0);
  const [phase, setPhase] = useState<"services" | "model" | "done" | "error">("services");
  const [services, setServices] = useState(initial.services);
  const [model, setModel] = useState<JcodeModelStatus | null>(null);
  const latest = useRef<JcodePowerStatus | null>(initial);
  const loadStart = useRef(0);
  const [now, setNow] = useState(() => Date.now());
  const shownPct = useRef(0);

  // Drive the whole transition once any confirm gate is cleared. Runs once (guarded by a
  // ref) so a re-render mid-sequence never restarts it.
  const started = useRef(false);
  useEffect(() => {
    if (!confirmed || started.current) return;
    started.current = true;
    let stale = false;
    const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

    async function bringUp() {
      try {
        latest.current = await api.jcodeSetPower(true);
        setServices(latest.current.services);
      } catch {
        if (!stale) setPhase("error");
        return;
      }
      // Poll until every service reports running (docker start → the port listening).
      for (let i = 0; i < 30 && !stale; i++) {
        const p = await api.jcodePower().catch(() => null);
        if (stale) return;
        if (p) {
          latest.current = p;
          setServices(p.services);
          if (p.on) break;
        }
        await sleep(2000);
      }
      if (stale) return;
      // Warm the coder onto the box and track the load bar (the same path the session
      // screen uses). Best-effort: the poll below reflects state even if the kick fails.
      setPhase("model");
      loadStart.current = Date.now();
      try {
        setModel(await api.jcodeWarmModel());
      } catch {
        /* ignore — the poll still reports residency */
      }
      for (let i = 0; i < 120 && !stale; i++) {
        const m = await api.jcodeModelStatus().catch(() => null);
        if (stale) return;
        if (m) {
          setModel(m);
          if (!m.hosting || (m.loaded && !m.warming)) break;
        }
        await sleep(2000);
      }
      if (stale) return;
      latest.current = await api.jcodePower().catch(() => latest.current);
      setPhase("done");
    }

    async function bringDown() {
      try {
        latest.current = await api.jcodeSetPower(false);
        setServices(latest.current.services);
      } catch {
        if (!stale) setPhase("error");
        return;
      }
      if (!stale) setPhase("done");
    }

    void (powerOn ? bringUp() : bringDown());
    return () => {
      stale = true;
    };
  }, [confirmed, powerOn]);

  // Tick a clock while the model loads so the fallback estimate advances between polls.
  useEffect(() => {
    if (phase !== "model") return;
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, [phase]);

  // Prefer the gateway's real load fraction; fall back to a size-based time estimate. The
  // bar never slides backwards and completes only when the load goes resident.
  const sizeGb = model?.size_gb ?? initial.size_gb;
  const elapsedSec = (now - loadStart.current) / 1000;
  const estPct =
    sizeGb > 0
      ? Math.min(96, Math.round((elapsedSec / (sizeGb * POWER_LOAD_SEC_PER_GB)) * 100))
      : 0;
  const realPct =
    model?.progress != null ? Math.min(99, Math.max(0, Math.round(model.progress * 100))) : null;
  if (phase === "model") shownPct.current = Math.max(shownPct.current, realPct ?? estPct);
  const loadPct = shownPct.current;

  const title = powerOn ? "Powering on code mode" : "Powering off code mode";

  return (
    // biome-ignore lint/a11y/useSemanticElements: a lightweight overlay panel, not a native <dialog>
    <div className="jcode-modal" role="dialog" aria-modal="true" aria-label={title}>
      <div className="jcode-modal-card">
        <div className="jcode-modal-head">
          <span>{title}</span>
          {(phase === "done" || phase === "error" || !confirmed) && (
            <button
              type="button"
              className="icon-btn"
              aria-label="Close"
              onClick={() => onClose(latest.current)}
            >
              ✕
            </button>
          )}
        </div>

        {!confirmed ? (
          <>
            <p className="jcode-empty">
              {initial.live_sessions} session{initial.live_sessions === 1 ? "" : "s"} still running
              — powering off stops the services and halts their shells (your checkouts are
              preserved). Continue?
            </p>
            <div className="jcode-actions">
              <button type="button" className="jcode-act" onClick={() => onClose(null)}>
                Cancel
              </button>
              <button type="button" className="jcode-act danger" onClick={() => setConfirmed(true)}>
                Power off
              </button>
            </div>
          </>
        ) : phase === "error" ? (
          <>
            <p className="jcode-empty jcode-share-error">
              Couldn't reach the supervisor to toggle the services. Try again.
            </p>
            <div className="jcode-actions">
              <button type="button" className="jcode-act" onClick={() => onClose(latest.current)}>
                Close
              </button>
            </div>
          </>
        ) : (
          <>
            <ul className="jcode-power-steps" aria-label="Services">
              {services.map((s) => (
                <li
                  key={s.name}
                  className={`jcode-power-step${s.running === powerOn ? " done" : ""}`}
                >
                  <span className="jcode-power-stepdot" />
                  {s.name}
                  <span className="jcode-power-stepstate">{s.running ? "running" : "stopped"}</span>
                </li>
              ))}
            </ul>

            {powerOn && phase === "model" && (
              <div className="jcode-modelload">
                <div className="jcode-modelload-row">
                  <span>Loading {model?.model ?? "the coder"} onto the box…</span>
                  <span className="jcode-modelload-pct">{loadPct}%</span>
                </div>
                <div className="jcode-modelload-track">
                  <div className="jcode-modelload-fill" style={{ width: `${loadPct}%` }} />
                </div>
              </div>
            )}

            {phase === "done" ? (
              <div className="jcode-actions">
                <button
                  type="button"
                  className="jcode-act teal"
                  onClick={() => onClose(latest.current)}
                >
                  Done
                </button>
              </div>
            ) : (
              <p className="jcode-empty">
                {phase === "services"
                  ? powerOn
                    ? "Starting services…"
                    : "Stopping services…"
                  : "This takes about a minute the first time."}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// A session row with the same swipe-left rail the agent-sessions manager uses
// (reusing notes/swipe): swipe reveals Rename (inline edit), Archive/Unarchive, and
// Delete (tap-again confirm). Tapping the row opens the session.
function JcodeSessionRow({
  session,
  onOpen,
  onRename,
  onArchive,
  onUnarchive,
  onDelete,
  railOpen,
  onRailChange,
}: {
  session: JcodeSession;
  onOpen: (s: JcodeSession) => void;
  onRename: (id: string, title: string) => void;
  onArchive: (id: string) => void;
  onUnarchive: (id: string) => void;
  onDelete: (id: string) => void;
  railOpen: boolean;
  onRailChange: (open: boolean) => void;
}): ReactNode {
  const [drag, setDrag] = useState<Drag | null>(null);
  const dragged = useRef(false);
  const renameRef = useRef<HTMLInputElement>(null);
  const [confirming, setConfirming] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(session.title);

  useEffect(() => {
    if (!railOpen) setConfirming(false);
  }, [railOpen]);

  // Land focus in the rename field without `autoFocus` (a11y).
  useEffect(() => {
    if (renaming) renameRef.current?.focus();
  }, [renaming]);

  const dragging = drag !== null && drag.axis === "h";
  const offset = renaming ? 0 : dragging ? drag.offset : railOpen ? -RAIL_WIDTH : 0;

  function onTouchStart(event: TouchEvent): void {
    if (renaming) return;
    dragged.current = false;
    const t = event.touches[0];
    if (t) setDrag(beginDrag(t.clientX, t.clientY, railOpen));
  }
  function onTouchMove(event: TouchEvent): void {
    if (drag === null) return;
    const t = event.touches[0];
    if (!t) return;
    const next = moveDrag(drag, t.clientX, t.clientY);
    if (next.axis === "v") {
      setDrag(null);
      return;
    }
    setDrag(next);
  }
  function onTouchEnd(): void {
    if (drag === null) return;
    if (drag.axis === "h") {
      dragged.current = true;
      onRailChange(endDrag(drag));
    }
    setDrag(null);
  }

  function onTap(): void {
    if (dragged.current) {
      dragged.current = false;
      return;
    }
    if (railOpen) {
      onRailChange(false);
      return;
    }
    onOpen(session);
  }

  function submitRename(): void {
    const title = draft.trim();
    setRenaming(false);
    onRailChange(false);
    if (title !== session.title) onRename(session.id, title);
  }

  return (
    <div className="jcode-rowwrap">
      {!renaming && offset < 0 && (
        <div className="jcode-rail rail-3">
          <button
            type="button"
            className="rail-btn rail-edit"
            onClick={() => {
              setDraft(session.title);
              setRenaming(true);
            }}
          >
            <PencilIcon size={16} />
            rename
          </button>
          <button
            type="button"
            className="rail-btn rail-archive"
            onClick={() => {
              onRailChange(false);
              if (session.archived) {
                onUnarchive(session.id);
              } else {
                onArchive(session.id);
              }
            }}
          >
            <JcodeArchiveGlyph />
            {session.archived ? "unarchive" : "archive"}
          </button>
          <button
            type="button"
            className={`rail-btn rail-delete${confirming ? " rail-armed" : ""}`}
            onClick={() => {
              if (!confirming) {
                setConfirming(true);
                return;
              }
              onRailChange(false);
              onDelete(session.id);
            }}
          >
            {confirming ? (
              "tap again"
            ) : (
              <>
                <TrashIcon size={16} />
                delete
              </>
            )}
          </button>
        </div>
      )}
      <div
        className="jcode-slide"
        style={{ transform: `translateX(${offset}px)` }}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        {renaming ? (
          <input
            ref={renameRef}
            className="jcode-rename"
            aria-label="Session title"
            placeholder={session.repo || "scratch"}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={submitRename}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submitRename();
              } else if (e.key === "Escape") {
                setRenaming(false);
              }
            }}
          />
        ) : (
          <button type="button" className="jcode-row" onClick={onTap}>
            <span
              className={`jcode-sd${session.status === "stopped" ? "" : " live"}`}
              title={session.status}
            />
            <span className="jcode-main">
              <span className="jcode-repo">{session.title || session.repo || "scratch"}</span>
              <span className="jcode-sub">
                @ {session.work_branch || session.branch} · {relative(session.last_active_at)}
              </span>
            </span>
            <span className="jcode-status">
              {session.status === "stopped" ? "stopped" : "ready"}
            </span>
            <ChevronRightIcon size={16} />
          </button>
        )}
      </div>
    </div>
  );
}

// A small archive-box glyph for the rail (icons.tsx has no archive icon); sized to
// match the 16px PencilIcon/TrashIcon beside it.
function JcodeArchiveGlyph(): ReactNode {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="4" width="18" height="4" rx="1" />
      <path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8" />
      <path d="M10 12h4" />
    </svg>
  );
}

function NewSessionSheet({
  onClose,
  onStart,
  onStartExternal,
}: {
  onClose: () => void;
  onStart: (input: NewSessionInput) => Promise<void>;
  onStartExternal: (label: string) => Promise<void>;
}) {
  const [mode, setMode] = useState<"clone" | "scratch" | "external">("clone");
  const [repo, setRepo] = useState("");
  const [workBranch, setWorkBranch] = useState("");
  const [extLabel, setExtLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function go() {
    setBusy(true);
    setError(null);
    try {
      if (mode === "external") {
        await onStartExternal(extLabel.trim() || "external session");
      } else {
        await onStart({
          repo: mode === "clone" ? repo.trim() : "",
          branch: "main",
          work_branch: workBranch.trim(),
        });
      }
    } catch {
      setError(
        mode === "external" ? "Couldn't create the endpoint." : "Couldn't start the session.",
      );
      setBusy(false);
    }
  }

  return (
    <Sheet title="New session" onClose={onClose}>
      <div className="jcode-seg" role="tablist" aria-label="Workspace">
        {(["clone", "scratch", "external"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={mode === m}
            className={`jcode-segbtn${mode === m ? " on" : ""}`}
            onClick={() => setMode(m)}
          >
            {m === "clone" ? "Clone repo" : m === "scratch" ? "Scratch" : "External"}
          </button>
        ))}
      </div>

      {mode === "external" ? (
        <>
          <input
            className="jcode-inp"
            placeholder="label (e.g. Claude on my laptop)"
            value={extLabel}
            onChange={(e) => setExtLabel(e.target.value)}
          />
          <div className="jcode-modelcard">
            <span className="jcode-mcname">Qwen3-Coder-Next 80B-A3B</span>
            <span className="jcode-mcbadge">on-box</span>
          </div>
          <p className="jcode-mcnote">
            A token-gated public endpoint exposing your loaded coder over the Anthropic API. Off
            until you switch it on; refuses when the coder isn't loaded. Revoke anytime.
          </p>
        </>
      ) : (
        <>
          {mode === "clone" && (
            <input
              className="jcode-inp"
              placeholder="https://github.com/you/repo"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
            />
          )}
          <input
            className="jcode-inp"
            placeholder="work branch (default: jcode/<id>)"
            value={workBranch}
            onChange={(e) => setWorkBranch(e.target.value)}
          />
          <div className="jcode-modelcard">
            <span className="jcode-mcname">Qwen3-Coder-Next 80B-A3B</span>
            <span className="jcode-mcbadge">on-box</span>
          </div>
          <p className="jcode-mcnote">
            Fresh isolated checkout · no host access · reads no notes · completions on-box.
          </p>
        </>
      )}

      {error && <p className="jcode-err">{error}</p>}
      <button
        type="button"
        className="jcode-start"
        disabled={busy || (mode === "clone" && !repo.trim())}
        onClick={go}
      >
        {busy ? (mode === "external" ? "Creating…" : "Starting…") : "Start session →"}
      </button>
    </Sheet>
  );
}
