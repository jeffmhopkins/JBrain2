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
import type { ExternalSession, JcodeSession, NewSessionInput } from "../jcode/types";
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

  async function refresh() {
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
                  onOpen={setOpen}
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
    </section>
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
              className={`jcode-sd${session.status === "running" ? " live" : ""}`}
              title={session.status}
            />
            <span className="jcode-main">
              <span className="jcode-repo">{session.title || session.repo || "scratch"}</span>
              <span className="jcode-sub">
                @ {session.work_branch || session.branch} · {relative(session.last_active_at)}
              </span>
            </span>
            {session.status === "running" ? (
              <span className="jcode-running">running…</span>
            ) : (
              <span className="jcode-status">{session.status}</span>
            )}
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
