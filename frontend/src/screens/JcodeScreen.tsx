// Code mode (jcode) — the resume-first launcher (docs/DESIGN.md "jcode", variant B).
// The session list is the hero (Chats-picker paradigm); "New session" opens setup in
// a bottom sheet. Tapping a row opens the tabbed session screen, which this screen
// stacks over itself (its own back returns to the list). A self-contained full-screen
// overlay like Tasks/Automations.

import { useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import { Sheet } from "../components/Sheet";
import { ChevronLeftIcon, PlusIcon } from "../components/icons";
import type { JcodeSession, NewSessionInput } from "../jcode/types";
import { JcodeSessionScreen } from "./JcodeSessionScreen";

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
  const min = Math.round(ms / 60000);
  if (min < 1) return "now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function JcodeScreen({ onClose }: { onClose: () => void }) {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [bucket, setBucket] = useState<"today" | "older">("today");
  const [newOpen, setNewOpen] = useState(false);
  const [open, setOpen] = useState<JcodeSession | null>(null);

  async function refresh() {
    try {
      setState({ kind: "ready", sessions: await api.jcodeSessions() });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setState({ kind: "disabled" });
      else setState({ kind: "error" });
    }
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

  const sessions = state.kind === "ready" ? state.sessions : [];
  const today = sessions.filter((s) => isToday(s.last_active_at));
  const older = sessions.filter((s) => !isToday(s.last_active_at));
  const rows = bucket === "today" ? today : older;

  async function start(input: NewSessionInput) {
    const session = await api.jcodeCreateSession(input);
    setNewOpen(false);
    setOpen(session);
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
      </header>

      {state.kind === "disabled" ? (
        <p className="jcode-empty">Code mode isn't enabled on this server.</p>
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

          <div className="jcode-buckets" role="tablist" aria-label="Sessions">
            {(["today", "older"] as const).map((b) => (
              <button
                key={b}
                type="button"
                role="tab"
                aria-selected={bucket === b}
                className={`jcode-bk${bucket === b ? " on" : ""}`}
                onClick={() => setBucket(b)}
              >
                {b === "today" ? "Today" : "Older"}
                <span className="jcode-bkpill">{b === "today" ? today.length : older.length}</span>
              </button>
            ))}
          </div>

          {state.kind === "loading" ? (
            <p className="jcode-empty">Loading…</p>
          ) : rows.length === 0 ? (
            <p className="jcode-empty">No {bucket} sessions — tap New session to start.</p>
          ) : (
            <div className="jcode-rows">
              {rows.map((s) => (
                <button key={s.id} type="button" className="jcode-row" onClick={() => setOpen(s)}>
                  <span
                    className={`jcode-sd${s.status === "running" ? " live" : ""}`}
                    title={s.status}
                  />
                  <span className="jcode-main">
                    <span className="jcode-repo">{s.repo || "scratch"}</span>
                    <span className="jcode-sub">
                      @ {s.work_branch || s.branch} · {relative(s.last_active_at)}
                    </span>
                  </span>
                  {s.status === "running" ? (
                    <span className="jcode-running">running…</span>
                  ) : (
                    <span className="jcode-status">{s.status}</span>
                  )}
                  <ChevronLeftIcon size={16} />
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {newOpen && <NewSessionSheet onClose={() => setNewOpen(false)} onStart={start} />}
    </section>
  );
}

function NewSessionSheet({
  onClose,
  onStart,
}: {
  onClose: () => void;
  onStart: (input: NewSessionInput) => Promise<void>;
}) {
  const [mode, setMode] = useState<"clone" | "scratch">("clone");
  const [repo, setRepo] = useState("");
  const [workBranch, setWorkBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function go() {
    setBusy(true);
    setError(null);
    try {
      await onStart({
        repo: mode === "clone" ? repo.trim() : "",
        branch: "main",
        work_branch: workBranch.trim(),
      });
    } catch {
      setError("Couldn't start the session.");
      setBusy(false);
    }
  }

  return (
    <Sheet title="New session" onClose={onClose}>
      <div className="jcode-seg" role="tablist" aria-label="Workspace">
        {(["clone", "scratch"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={mode === m}
            className={`jcode-segbtn${mode === m ? " on" : ""}`}
            onClick={() => setMode(m)}
          >
            {m === "clone" ? "Clone repo" : "Scratch"}
          </button>
        ))}
      </div>
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
      {error && <p className="jcode-err">{error}</p>}
      <button
        type="button"
        className="jcode-start"
        disabled={busy || (mode === "clone" && !repo.trim())}
        onClick={go}
      >
        {busy ? "Starting…" : "Start session →"}
      </button>
    </Sheet>
  );
}
