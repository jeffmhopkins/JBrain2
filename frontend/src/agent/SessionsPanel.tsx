// The Sessions panel (left swipe from Full Brain): the capability records, newest
// first, and the new-session picker. A session's read scope is chosen up front
// and least-privilege by default — picking domains here sets the firewall the
// session's tools read under (docs/ASSISTANT.md "Session capabilities").

import { type ReactNode, type TouchEvent, useEffect, useRef, useState } from "react";
import { Sheet } from "../components/Sheet";
import { PencilIcon, TrashIcon } from "../components/icons";
import { type Drag, RAIL_WIDTH, beginDrag, endDrag, moveDrag } from "../notes/swipe";
import type { AgentSession, SessionCreate } from "./types";

const DOMAINS: { code: string; label: string; desc: string }[] = [
  { code: "general", label: "General", desc: "notes, lists, wiki" },
  { code: "health", label: "Health", desc: "labs, meds, medical notes" },
  { code: "finance", label: "Finance", desc: "statements, receipts" },
  { code: "location", label: "Location", desc: "places, geofences" },
];

interface Props {
  sessions: AgentSession[];
  onOpen: (session: AgentSession) => void;
  onCreate: (body: SessionCreate) => Promise<AgentSession>;
  onClose: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}

export function SessionsPanel({
  sessions,
  onOpen,
  onCreate,
  onClose,
  onRename,
  onDelete,
}: Props): ReactNode {
  const [picking, setPicking] = useState(false);
  // One swipe rail open at a time (like the home stream).
  const [railId, setRailId] = useState<string | null>(null);
  const active = sessions.filter((s) => s.status === "active");
  const ended = sessions.filter((s) => s.status !== "active");

  const row = (s: AgentSession): ReactNode => (
    <SessionRow
      key={s.id}
      session={s}
      onOpen={onOpen}
      onRename={onRename}
      onDelete={onDelete}
      railOpen={railId === s.id}
      onRailChange={(open) => setRailId(open ? s.id : null)}
    />
  );

  return (
    <section className="panel-content" aria-label="Sessions">
      <div className="panel-bar">
        <button type="button" className="back" aria-label="Back to chat" onClick={onClose}>
          ‹
        </button>
        <span className="ttl">Sessions</span>
        <span className="sub">read-scope chosen at start</span>
      </div>
      <div className="panel-body">
        <button type="button" className="row new-session" onClick={() => setPicking(true)}>
          ＋ New session — choose sources
        </button>

        {active.length > 0 && <div className="sect">Active</div>}
        {active.map(row)}

        {ended.length > 0 && <div className="sect">Earlier</div>}
        {ended.map(row)}

        {sessions.length === 0 && (
          <div className="panel-empty">No sessions yet — start one to ask about your brain.</div>
        )}
      </div>

      {picking && (
        <NewSessionSheet
          onClose={() => setPicking(false)}
          onCreate={async (body) => {
            const created = await onCreate(body);
            setPicking(false);
            onOpen(created);
          }}
        />
      )}
    </section>
  );
}

// A session row with the same swipe-left action rail the home notes use (reusing
// notes/swipe): swipe reveals Rename (inline edit) and Delete (tap-again confirm).
function SessionRow({
  session,
  onOpen,
  onRename,
  onDelete,
  railOpen,
  onRailChange,
}: {
  session: AgentSession;
  onOpen: (s: AgentSession) => void;
  onRename: (id: string, title: string) => void;
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

  // stopPropagation so the row's swipe doesn't reach the shell's panel gestures.
  function onTouchStart(event: TouchEvent): void {
    if (renaming) return;
    event.stopPropagation();
    dragged.current = false;
    const t = event.touches[0];
    if (t) setDrag(beginDrag(t.clientX, t.clientY, railOpen));
  }
  function onTouchMove(event: TouchEvent): void {
    if (drag === null) return;
    event.stopPropagation();
    const t = event.touches[0];
    if (!t) return;
    const next = moveDrag(drag, t.clientX, t.clientY);
    if (next.axis === "v") {
      setDrag(null);
      return;
    }
    setDrag(next);
  }
  function onTouchEnd(event: TouchEvent): void {
    if (drag === null) return;
    event.stopPropagation();
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
    if (title && title !== session.title) onRename(session.id, title);
  }

  return (
    <div className="session-wrap">
      {!renaming && offset < 0 && (
        <div className="session-rail">
          <button
            type="button"
            className="rail-btn rail-edit"
            onClick={() => {
              setDraft(session.title);
              setRenaming(true);
            }}
          >
            <PencilIcon size={18} />
            rename
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
                <TrashIcon size={18} />
                delete
              </>
            )}
          </button>
        </div>
      )}
      <div
        className="row session-slide"
        style={{ transform: `translateX(${offset}px)` }}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        {renaming ? (
          <input
            ref={renameRef}
            className="session-rename"
            aria-label="Session title"
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
          <button type="button" className="session-tap" onClick={onTap}>
            <div className="r-head">{session.title || "Untitled session"}</div>
            <div className="pills">
              {session.domain_scopes.map((d) => (
                <span key={d} className={`pill ${d}`}>
                  {d}
                </span>
              ))}
              {session.domain_scopes.length === 0 && <span className="pill">all domains</span>}
            </div>
          </button>
        )}
      </div>
    </div>
  );
}

function NewSessionSheet({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (body: SessionCreate) => void | Promise<void>;
}): ReactNode {
  // Least-privilege default: general only. Widening is an explicit act.
  const [selected, setSelected] = useState<Set<string>>(new Set(["general"]));
  const [title, setTitle] = useState("");

  function toggle(code: string): void {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
      }
      return next;
    });
  }

  const chosen = DOMAINS.filter((d) => selected.has(d.code)).map((d) => d.code);

  return (
    <Sheet title="New session" onClose={onClose}>
      <p className="lead">
        Choose which knowledge sources this session may <b>read</b>. Narrow by default — widen only
        when you need to. This sets the session's firewall.
      </p>
      <input
        className="session-title"
        aria-label="Session title"
        placeholder="Title (optional)"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
      />
      <div className="domain-opts">
        {DOMAINS.map((d) => (
          <button
            type="button"
            key={d.code}
            className={`opt${selected.has(d.code) ? " on" : ""}`}
            aria-pressed={selected.has(d.code)}
            onClick={() => toggle(d.code)}
          >
            <span className="opt-t">{d.label}</span>
            <span className="opt-d">{d.desc}</span>
          </button>
        ))}
      </div>
      <button
        type="button"
        className="start"
        disabled={chosen.length === 0}
        onClick={() => void onCreate({ domain_scopes: chosen, title: title.trim() })}
      >
        Start session · reads {chosen.length ? chosen.join(" · ") : "nothing"}
      </button>
      <p className="writes-note">
        Reads only. Any change the agent wants is <b>staged as a Proposal</b> for your approval.
      </p>
    </Sheet>
  );
}
