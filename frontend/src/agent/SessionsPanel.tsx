// The Chats panel (left swipe from Full Brain): the capability records, newest
// first, and the new-chat picker. Read scope is a rail you nudge, not a gate you
// climb — a new chat starts in one tap on your last-used scope (seeded to all
// domains for the owner, who holds every scope; ASSISTANT.md "Session
// capabilities"), and named presets hide the per-domain grid until you ask for it
// (docs/mocks/session-panel-b-quick-presets.html). The selection still sets the
// RLS domain-scope GUC the session's tools read under.

import { type ReactNode, type TouchEvent, useEffect, useRef, useState } from "react";
import { Sheet } from "../components/Sheet";
import { PencilIcon, SearchIcon, TrashIcon } from "../components/icons";
import { type Drag, RAIL_WIDTH, beginDrag, endDrag, moveDrag } from "../notes/swipe";
import type { AgentSession, SessionCreate } from "./types";

// Backend domain codes paired with their calm display labels (DESIGN.md speaks
// "Medical/Financial", never the raw `health`/`finance` codes the wire carries).
const DOMAINS: { code: string; label: string; desc: string }[] = [
  { code: "general", label: "General", desc: "notes, lists, wiki" },
  { code: "health", label: "Medical", desc: "labs, meds, medical notes" },
  { code: "finance", label: "Financial", desc: "statements, receipts" },
  { code: "location", label: "Location", desc: "places, geofences" },
];
const ALL = DOMAINS.map((d) => d.code);
const byCode = (code: string) => DOMAINS.find((d) => d.code === code);

// Named presets map onto domain-code sets; `set: null` is the Custom escape hatch
// that reveals the per-domain grid. "Medical"/"Financial" carry general too, so a
// domain chat can still read the everyday spine.
const PRESETS: { id: string; label: string; set: string[] | null }[] = [
  { id: "everything", label: "Everything", set: [...ALL] },
  { id: "general", label: "General", set: ["general"] },
  { id: "medical", label: "Medical", set: ["general", "health"] },
  { id: "financial", label: "Financial", set: ["general", "finance"] },
  { id: "custom", label: "Custom…", set: null },
];

// The selectable agents at session start (docs/ASSISTANT.md "Agent selection").
// Only the curator reads the knowledge base; teacher and jerv read no owner data,
// so the scope dial is hidden for them and the chat is created with empty scopes —
// the firewall, not just a label. `note` is the caveat shown under a no-data agent.
interface AgentChoice {
  id: string;
  label: string;
  desc: string;
  readsKb: boolean;
  note: string;
  // The terse caption under the Start button for a no-data agent (readsKb agents
  // show their read scope there instead). Each agent owns its own line so the
  // Start pill matches the selection rather than falling through to a default.
  hint: string;
}
const CURATOR_AGENT: AgentChoice = {
  id: "curator",
  label: "Curator",
  desc: "Your Full Brain — searches your notes, facts, lists and appointments.",
  readsKb: true,
  note: "",
  hint: "",
};
const AGENTS: AgentChoice[] = [
  CURATOR_AGENT,
  {
    id: "teacher",
    label: "Teacher",
    desc: "A Socratic homework tutor — guides you to the answer instead of handing it over.",
    readsKb: false,
    note: "Teaches from this conversation only — no access to your notes or data.",
    hint: "no data — a study tutor",
  },
  {
    id: "jerv",
    label: "Jerv",
    desc: "A web chatbot — searches and reads the open internet to answer you.",
    readsKb: false,
    note: "Talks to the open web. No access to your notes or any of your data.",
    hint: "reads the web, not your notes",
  },
  {
    id: "archivist",
    label: "Archivist",
    desc: "Organizes your Gmail — searches, counts, labels and archives email into a clean taxonomy.",
    readsKb: false,
    note: "Works only in your Gmail (read, label, archive — never deletes). No access to your notes or other data.",
    hint: "your Gmail only, not your notes",
  },
];
const agentById = (id: string): AgentChoice => AGENTS.find((a) => a.id === id) ?? CURATOR_AGENT;

// The CSS scope-chip class for a single domain code (display, not the wire code).
const SCOPE_CLASS: Record<string, string> = {
  general: "general",
  health: "medical",
  finance: "financial",
  location: "location",
};

// Read-scope rendered as one calm chip rather than a wall of domain pills:
// "everything", a single domain's preset name, or a comma list for odd sets.
function scopeKind(scope: string[]): { cls: string; label: string } {
  const set = new Set(scope);
  if (set.size === 0) return { cls: "custom", label: "nothing" };
  if (ALL.every((c) => set.has(c))) return { cls: "everything", label: "everything" };
  // A lone domain, or general + exactly one extra, both read as that domain's
  // preset (e.g. Medical = general + health).
  const extra = scope.filter((c) => c !== "general");
  const sole =
    scope.length === 1 ? scope[0] : set.has("general") && extra.length === 1 ? extra[0] : null;
  if (sole) {
    return {
      cls: SCOPE_CLASS[sole] ?? "custom",
      label: (byCode(sole)?.label ?? sole).toLowerCase(),
    };
  }
  return {
    cls: "custom",
    label: scope.map((c) => (byCode(c)?.label ?? c).toLowerCase()).join(", "),
  };
}

// Which preset produced this scope set (else Custom) — used to seed the sheet.
function scopeToPreset(scope: string[]): string {
  for (const p of PRESETS) {
    if (p.set && p.set.length === scope.length && p.set.every((c) => scope.includes(c))) {
      return p.id;
    }
  }
  return "custom";
}

const LAST_SCOPE_KEY = "jb.fb.lastScope";

// The smart default: what the last chat read. First run has none, so the owner —
// who already holds every scope — starts wide; narrowing then sticks as remembered
// intent (ASSISTANT.md: "a deliberate minimal/last-used set"). RLS, writes-staging,
// and the egress chokepoint are the real boundary; this dial is just convenience.
function readLastScope(): string[] {
  try {
    const raw = localStorage.getItem(LAST_SCOPE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (Array.isArray(parsed)) {
      const codes = parsed.filter((c): c is string => typeof c === "string" && ALL.includes(c));
      if (codes.length) return codes;
    }
  } catch {
    // fall through to the default
  }
  return [...ALL];
}
function writeLastScope(scope: string[]): void {
  try {
    localStorage.setItem(LAST_SCOPE_KEY, JSON.stringify(scope));
  } catch {
    // best-effort; a missing last-used just re-seeds wide next time
  }
}

// The picker buckets chats into three segments — Today · Older · Archived — shown
// one at a time so the list stays short. "Older" folds yesterday and everything
// before it together (off last_active_at).
type TabId = "today" | "older" | "archived";
const startOfDay = (d: Date): number =>
  new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
function isToday(iso: string, now: Date): boolean {
  return startOfDay(new Date(iso)) >= startOfDay(now);
}

// Above this many chats, the search field earns its place; below it, it's clutter.
const SEARCH_THRESHOLD = 6;
// A sub-agent rail collapses by default once a fan grows past this, so a big fan
// doesn't bury the rest of the (now dense) Chats list (review M10).
const RAIL_COLLAPSE_THRESHOLD = 3;

interface Props {
  sessions: AgentSession[];
  /** The agents a new chat may use here — the tab's group (Research offers Jerv +
   * Teacher; Full Brain only Curator). Omitted = all three (legacy/standalone). */
  agentOptions?: readonly string[];
  /** The chat currently open in the surface, marked "you are here". */
  activeId?: string | null;
  /** The chat with a turn streaming right now (and what it's doing) — drives the
   * animated activity glyph on that row, so an in-flight thinking/render is visible
   * even while another chat is open. */
  activeTurn?: { sessionId: string; kind: "thinking" | "rendering" } | null;
  onOpen: (session: AgentSession) => void;
  onCreate: (body: SessionCreate) => Promise<AgentSession>;
  onClose: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onArchive: (id: string) => void;
  onUnarchive: (id: string) => void;
  onRescope: (id: string, domainScopes: string[]) => void;
}

export function SessionsPanel({
  sessions,
  agentOptions = ["curator", "teacher", "jerv"],
  activeId,
  activeTurn,
  onOpen,
  onCreate,
  onClose,
  onRename,
  onDelete,
  onArchive,
  onUnarchive,
  onRescope,
}: Props): ReactNode {
  // The new-chat agents in the order they're offered — the first is the default,
  // so Research seeds Jerv and Full Brain seeds Curator.
  const newAgents = agentOptions
    .map((id) => AGENTS.find((a) => a.id === id))
    .filter((a): a is AgentChoice => a !== undefined);
  const [picking, setPicking] = useState(false);
  const [query, setQuery] = useState("");
  // The segment the owner explicitly picked, or null to follow the data (so the
  // picker never lands on an empty Today while chats load into Older/Archived).
  const [tab, setTab] = useState<TabId | null>(null);
  // The chat whose scope is being edited (rail "scope" tapped), or null.
  const [rescoping, setRescoping] = useState<AgentSession | null>(null);
  // One swipe rail open at a time (like the home stream).
  const [railId, setRailId] = useState<string | null>(null);

  const q = query.trim().toLowerCase();
  const matches = (s: AgentSession): boolean =>
    q === "" || (s.title || "untitled chat").toLowerCase().includes(q);

  // Sub-agent children are sub-state of the chat that spawned them: they appear ONLY
  // nested under their parent (docs/SUBAGENT_SPAWNING_PLAN.md Wave S4), never as their
  // own top-level rows. Index them by parent so a chat can render its rail.
  const childrenByParent = new Map<string, AgentSession[]>();
  for (const s of sessions) {
    if (s.parent_session_id) {
      const arr = childrenByParent.get(s.parent_session_id);
      if (arr) arr.push(s);
      else childrenByParent.set(s.parent_session_id, [s]);
    }
  }
  // A child is nested under its parent ONLY if that parent is itself in the list; a
  // child whose parent is absent (a stale/transient mismatch — true orphans can't
  // persist, parent_session_id is ON DELETE CASCADE) falls back to a top-level row
  // rather than vanishing.
  const ids = new Set(sessions.map((s) => s.id));
  const isTopLevel = (s: AgentSession): boolean =>
    !s.parent_session_id || !ids.has(s.parent_session_id);

  // Newest-active first, then split into the three segments — top-level chats only
  // (nested children are excluded from bucketing; they appear under their parent).
  const now = new Date();
  const ordered = [...sessions]
    .filter(isTopLevel)
    .filter(matches)
    .sort((a, b) => Date.parse(b.last_active_at) - Date.parse(a.last_active_at));
  const groups: Record<TabId, AgentSession[]> = { today: [], older: [], archived: [] };
  for (const s of ordered) {
    if (s.status === "archived") groups.archived.push(s);
    else if (isToday(s.last_active_at, now)) groups.today.push(s);
    else groups.older.push(s);
  }
  const TAB_DEFS: { id: TabId; label: string }[] = [
    { id: "today", label: "Today" },
    { id: "older", label: "Older" },
    { id: "archived", label: "Archived" },
  ];
  // Until the owner taps a segment, show the first non-empty one.
  const effective: TabId =
    tab ??
    (groups.today.length
      ? "today"
      : groups.older.length
        ? "older"
        : groups.archived.length
          ? "archived"
          : "today");
  const activeRows = groups[effective];

  const row = (s: AgentSession): ReactNode => {
    const kids = childrenByParent.get(s.id) ?? [];
    const sessionRow = (
      <SessionRow
        session={s}
        active={s.id === activeId}
        {...(activeTurn?.sessionId === s.id ? { turn: activeTurn.kind } : {})}
        onOpen={onOpen}
        onRename={onRename}
        onDelete={onDelete}
        onArchive={onArchive}
        onUnarchive={onUnarchive}
        onEditScope={setRescoping}
        railOpen={railId === s.id}
        onRailChange={(open) => setRailId(open ? s.id : null)}
      />
    );
    if (kids.length === 0) return <div key={s.id}>{sessionRow}</div>;
    // A spawning chat: the chat row, then its sub-agent rail (the rail itself is the
    // tree, role="tree"). The whole fan animates live while the parent turn is the
    // active turn (fan-in A — children are sub-state of that one turn).
    return (
      <div key={s.id} className="sa-tree">
        {sessionRow}
        <SubagentRail
          parentId={s.id}
          childrenByParent={childrenByParent}
          live={activeTurn?.sessionId === s.id}
          collapsedDefault={
            (s.subagent_count ?? kids.length) > RAIL_COLLAPSE_THRESHOLD || s.status === "archived"
          }
          level={2}
          onOpen={onOpen}
        />
      </div>
    );
  };

  return (
    <section className="panel-content" aria-label="Chats">
      <div className="panel-bar">
        <button type="button" className="back" aria-label="Back to chat" onClick={onClose}>
          ‹
        </button>
        <span className="ttl">Chats</span>
        <span className="sub">tap one, or start fresh</span>
      </div>
      <div className="panel-top">
        <button type="button" className="row new-session" onClick={() => setPicking(true)}>
          ＋ New chat
        </button>

        <div className="seg-row chat-seg" aria-label="Chat groups">
          {TAB_DEFS.map((t) => (
            <button
              type="button"
              key={t.id}
              className={`seg${effective === t.id ? " seg-on" : ""}`}
              aria-pressed={effective === t.id}
              onClick={() => setTab(t.id)}
            >
              {t.label}
              <span className="seg-n">{groups[t.id].length}</span>
            </button>
          ))}
        </div>

        {sessions.length > SEARCH_THRESHOLD && (
          <div className="chat-search">
            <SearchIcon size={16} />
            <input
              type="search"
              aria-label="Search chats"
              placeholder="Search chats"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        )}
      </div>

      <div className="panel-list">
        {activeRows.length > 0 ? (
          <div className="chat-group">{activeRows.map(row)}</div>
        ) : (
          <div className="panel-empty">
            {sessions.length === 0
              ? "No chats yet — start one to ask about your brain."
              : q
                ? `No chats match “${query.trim()}”.`
                : effective === "today"
                  ? "Nothing today — older chats are a tap away."
                  : effective === "archived"
                    ? "No archived chats."
                    : "Nothing here yet."}
          </div>
        )}
      </div>

      {picking && (
        <ScopeSheet
          sheetTitle="New chat"
          lead="Pick who you're talking to — your data access follows the agent."
          seed={readLastScope()}
          actionLabel="Start"
          withTitle
          agents={newAgents}
          onClose={() => setPicking(false)}
          onSubmit={async (scope, title, agent) => {
            const created = await onCreate({
              domain_scopes: scope,
              title,
              ...(agent ? { agent } : {}),
            });
            setPicking(false);
            onOpen(created);
          }}
        />
      )}

      {rescoping && (
        <ScopeSheet
          sheetTitle="Change scope"
          lead="Adjust what this chat can read — scope is a rail you nudge, not frozen at the start."
          seed={rescoping.domain_scopes}
          actionLabel="Save scope"
          onClose={() => setRescoping(null)}
          onSubmit={(scope) => {
            onRescope(rescoping.id, scope);
            setRescoping(null);
          }}
        />
      )}
    </section>
  );
}

// A chat row with the same swipe-left action rail the home notes use (reusing
// notes/swipe): swipe reveals Rename (inline edit) and Delete (tap-again confirm).
function SessionRow({
  session,
  active,
  turn,
  onOpen,
  onRename,
  onDelete,
  onArchive,
  onUnarchive,
  onEditScope,
  railOpen,
  onRailChange,
}: {
  session: AgentSession;
  active: boolean;
  /** Set when this chat has a turn streaming now: its activity glyph + label. */
  turn?: "thinking" | "rendering";
  onOpen: (s: AgentSession) => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onArchive: (id: string) => void;
  onUnarchive: (id: string) => void;
  onEditScope: (s: AgentSession) => void;
  railOpen: boolean;
  onRailChange: (open: boolean) => void;
}): ReactNode {
  const isArchived = session.status === "archived";
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

  const scope = scopeKind(session.domain_scopes);

  return (
    <div className="session-wrap">
      {!renaming && offset < 0 && (
        <div className="session-rail rail-4">
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
            className="rail-btn rail-scope"
            onClick={() => {
              onRailChange(false);
              onEditScope(session);
            }}
          >
            <ScopeGlyph />
            scope
          </button>
          <button
            type="button"
            className="rail-btn rail-archive"
            onClick={() => {
              onRailChange(false);
              if (isArchived) {
                onUnarchive(session.id);
              } else {
                onArchive(session.id);
              }
            }}
          >
            <ArchiveGlyph />
            {isArchived ? "unarchive" : "archive"}
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
        className={`session-slide${active ? " live" : ""}`}
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
          // One compact line: a scope-tinted dot (green when this is the open chat),
          // the title, then turns/staged. Preview and the scope chip moved off the
          // row for density — scope lives behind the swipe rail's "scope" action.
          <button
            type="button"
            className="session-tap"
            onClick={onTap}
            aria-current={active ? "true" : undefined}
          >
            {turn ? (
              <TurnGlyph kind={turn} />
            ) : (
              <span
                className={`dot ${active ? "live" : scope.cls}`}
                title={active ? `open · reads ${scope.label}` : `reads ${scope.label}`}
              />
            )}
            <span className={`m-title${session.title ? "" : " untitled"}`}>
              {session.title || "Untitled chat"}
            </span>
            <span className="m-meta">
              {turn ? (
                // A live turn supersedes the turn/staged counts — say what it's doing.
                <span className="stat turn-status">
                  {turn === "rendering" ? "rendering…" : "thinking…"}
                </span>
              ) : (
                <>
                  {session.staged_count ? (
                    <span className="stat staged">{session.staged_count} staged</span>
                  ) : null}
                  {session.turn_count ? (
                    <span className="r-turns">
                      {session.turn_count} turn{session.turn_count === 1 ? "" : "s"}
                    </span>
                  ) : null}
                </>
              )}
            </span>
            <svg
              className="m-car"
              width="15"
              height="15"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M9 6l6 6-6 6" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}

// The session row's live-activity glyph, sitting where the leading scope dot would:
// bouncing dots while the agent is thinking, a twinkling spark while it renders an
// image. Purely DECORATIVE (aria-hidden) — the meaning is carried by the visible
// "thinking…/rendering…" word, which is part of the row button's accessible name, so a
// screen reader hears the state without a nagging live region. Motion honors
// prefers-reduced-motion in CSS (it falls back to a steady glyph).
function TurnGlyph({ kind }: { kind: "thinking" | "rendering" }): ReactNode {
  return (
    <span className={`turn-glyph ${kind}`} aria-hidden="true">
      {kind === "rendering" ? (
        <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M12 2l2.3 6.4L21 11l-6.7 2.6L12 20l-2.3-6.4L3 11l6.7-2.6z" />
        </svg>
      ) : (
        <>
          <i />
          <i />
          <i />
        </>
      )}
    </span>
  );
}

const PERSONA_LABEL: Record<string, string> = {
  research: "research",
  review: "review",
  summarize: "summarize",
};

// A spawning chat's sub-agent rail: the children nested under it, collapsed by
// default once the fan is large (or the parent is archived). The toggle is a real
// button with aria-expanded; the rail is a tree group of treeitem nodes.
function SubagentRail({
  parentId,
  childrenByParent,
  live,
  collapsedDefault,
  level,
  onOpen,
}: {
  parentId: string;
  childrenByParent: Map<string, AgentSession[]>;
  live: boolean;
  collapsedDefault: boolean;
  level: number;
  onOpen: (s: AgentSession) => void;
}): ReactNode {
  const kids = childrenByParent.get(parentId) ?? [];
  // The owner's explicit toggle wins; until they touch it, follow the prop so a
  // refetch that flips the default (archived, or the fan grew past the threshold)
  // re-applies — `useState` alone would go stale (the panel refetches per turn).
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? !collapsedDefault;
  const groupId = `sa-rail-${parentId}`;
  if (kids.length === 0) return null;
  // The roll-up shown on the (possibly collapsed) header, so a folded rail still says
  // what ran: live → N running; settled → done · N ran · M failed.
  const failed = kids.filter((k) => !live && k.last_run_status === "error").length;
  const rollup = live
    ? "running"
    : `done · ${kids.length} ran${failed ? ` · ${failed} failed` : ""}`;
  return (
    <div className="sa-rail">
      <button
        type="button"
        className="sa-rail-toggle"
        aria-expanded={open}
        aria-controls={groupId}
        onClick={() => setUserOpen(!open)}
      >
        <span aria-hidden="true">{open ? "▾" : "▸"}</span> sub-agents ({kids.length})
        <span className={`sa-rail-rollup${failed ? " fail" : ""}`}>· {rollup}</span>
      </button>
      {open && (
        <div id={groupId} className="sa-rail-kids" role="tree">
          {kids.map((k) => (
            <SubagentNode
              key={k.id}
              session={k}
              childrenByParent={childrenByParent}
              live={live}
              level={level}
              onOpen={onOpen}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// A child is failed iff its latest run errored (the in-tree settled outcome); the
// in-chat accordion carries the live/truncated detail. While the parent turn runs the
// whole fan reads as live (fan-in A); a settled child is done ✓ or failed rose ✕.
function childState(session: AgentSession, live: boolean): "running" | "failed" | "done" {
  if (live) return "running";
  return session.last_run_status === "error" ? "failed" : "done";
}

// The deepest the tree can nest (the depth<2 structural cap → child + grandchild).
// A hard recursion bound so a cyclic/anomalous parent_session_id can never spin the
// render, independent of the DB depth CHECK (which bounds the value, not cycles).
const MAX_RAIL_LEVEL = 4;

// One child (or grandchild) node in the rail. Neutral persona tag, never a colour;
// grandchildren nest one rail deeper (up to the hard level cap).
function SubagentNode({
  session,
  childrenByParent,
  live,
  level,
  onOpen,
}: {
  session: AgentSession;
  childrenByParent: Map<string, AgentSession[]>;
  live: boolean;
  level: number;
  onOpen: (s: AgentSession) => void;
}): ReactNode {
  const grandkids = level < MAX_RAIL_LEVEL ? (childrenByParent.get(session.id) ?? []) : [];
  const state = childState(session, live);
  return (
    <div className="sa-node" role="treeitem" aria-level={level} aria-label={session.title}>
      <button type="button" className="sa-node-line" onClick={() => onOpen(session)}>
        {state === "running" ? (
          <TurnGlyph kind="thinking" />
        ) : (
          <span className={`sa-node-glyph ${state}`} aria-hidden="true">
            {state === "failed" ? "✕" : "✓"}
          </span>
        )}
        <span className="sa-node-lbl">{session.title || "sub-agent"}</span>
        <span className="sa-ptag">{PERSONA_LABEL[session.agent] ?? session.agent}</span>
        <span className={`sa-node-st${state === "failed" ? " fail" : ""}`}>{state}</span>
      </button>
      {grandkids.length > 0 && (
        // biome-ignore lint/a11y/useSemanticElements: nested tree group
        <div className="sa-node-grail" role="group">
          {grandkids.map((g) => (
            <SubagentNode
              key={g.id}
              session={g}
              childrenByParent={childrenByParent}
              live={live}
              level={level + 1}
              onOpen={onOpen}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// A small archive-box glyph for the rail (icons.tsx has no archive icon); sized
// to match the 18px PencilIcon/TrashIcon beside it.
function ArchiveGlyph(): ReactNode {
  return (
    <svg
      width="18"
      height="18"
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

// Sliders glyph for the rail's "scope" action — re-scoping is nudging a dial.
function ScopeGlyph(): ReactNode {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h10M18 18h2" />
      <circle cx="16" cy="6" r="2" />
      <circle cx="8" cy="12" r="2" />
      <circle cx="16" cy="18" r="2" />
    </svg>
  );
}

// One sheet for both flows: starting a new chat and re-scoping an existing one.
// Read scope is the same dial in both — presets up front, the per-domain grid
// behind Custom — differing only in the verb and whether a title field shows.
function ScopeSheet({
  sheetTitle,
  lead,
  seed,
  actionLabel,
  withTitle = false,
  agents = [],
  onClose,
  onSubmit,
}: {
  sheetTitle: string;
  lead: string;
  /** Initial scope the pills/grid open on. */
  seed: string[];
  /** Primary-button verb: "Start" for a new chat, "Save scope" when re-scoping. */
  actionLabel: string;
  /** Show the optional title field (new chat only). */
  withTitle?: boolean;
  /** The agents this chat may use; the first is the seeded default. Empty means
   * no agent choice (re-scoping). A single agent rides the payload without a
   * picker; two or more show the picker — and a no-data agent hides the scope dial. */
  agents?: AgentChoice[];
  onClose: () => void;
  onSubmit: (scope: string[], title: string, agent?: string) => void | Promise<void>;
}): ReactNode {
  const [preset, setPreset] = useState<string>(() => scopeToPreset(seed));
  const [custom, setCustom] = useState<Set<string>>(() => new Set(seed));
  const [title, setTitle] = useState("");
  const [agent, setAgent] = useState(agents[0]?.id ?? "curator");

  // One agent rides the payload silently; two or more earn the picker.
  const includeAgent = agents.length > 0;
  const showAgentPicker = agents.length > 1;
  const currentAgent = agentById(agent);
  // A no-data agent (teacher/jerv) hides the scope dial and starts with empty
  // scopes; the RLS firewall — not this UI — is what makes that real.
  const showScope = !includeAgent || currentAgent.readsKb;
  const scope =
    preset === "custom" ? [...custom] : (PRESETS.find((p) => p.id === preset)?.set ?? []);
  const summary = scopeKind(scope);
  const canStart = showScope ? scope.length > 0 : true;
  const startHint = !showScope
    ? currentAgent.hint
    : `reads ${scope.length === 0 ? "nothing yet" : summary.label}`;

  function pick(id: string): void {
    // Entering Custom continues from whatever the pills currently read, so the
    // grid mirrors the active preset rather than jumping to a stale selection.
    if (id === "custom") {
      setCustom(new Set(scope.length ? scope : ["general"]));
    }
    setPreset(id);
  }

  function toggleDomain(code: string): void {
    setCustom((prev) => {
      const next = new Set(prev);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
      }
      return next;
    });
  }

  function submit(): void {
    const effScope = showScope ? scope : [];
    if (showScope && effScope.length === 0) return;
    if (effScope.length) writeLastScope(effScope); // the chosen scope seeds the next chat
    void onSubmit(effScope, title.trim(), includeAgent ? agent : undefined);
  }

  return (
    <Sheet title={sheetTitle} onClose={onClose}>
      <p className="lead">{lead}</p>

      {showAgentPicker && (
        <div className="agent-opts">
          {agents.map((a) => (
            <button
              type="button"
              key={a.id}
              className={`agent-opt ${a.id}${agent === a.id ? " on" : ""}`}
              aria-pressed={agent === a.id}
              onClick={() => setAgent(a.id)}
            >
              <span className="agent-opt-ico" aria-hidden="true">
                {a.label[0]}
              </span>
              <span className="agent-opt-meta">
                <span className="agent-opt-t">{a.label}</span>
                <span className="agent-opt-d">{a.desc}</span>
              </span>
              <span className="agent-opt-check" aria-hidden="true">
                ✓
              </span>
            </button>
          ))}
        </div>
      )}

      <button type="button" className="start-big" disabled={!canStart} onClick={submit}>
        <span className="start-main">{actionLabel}</span>
        <span className="start-hint">{startHint}</span>
      </button>

      {showScope ? (
        <>
          <div className="or">or choose a preset</div>
          <div className="presets">
            {PRESETS.map((p) => (
              <button
                type="button"
                key={p.id}
                className={`preset${preset === p.id ? " on" : ""}`}
                aria-pressed={preset === p.id}
                onClick={() => pick(p.id)}
              >
                {p.label}
              </button>
            ))}
          </div>

          <p className="reads-summary">
            {scope.length === 0 ? (
              <>
                reads <b>nothing yet</b> — pick at least one source.
              </>
            ) : summary.cls === "everything" ? (
              <>
                reads <b>everything</b> — all your notes.
              </>
            ) : (
              <>
                reads <b>{summary.label}</b>.
              </>
            )}
          </p>

          {preset === "custom" && (
            <div className="domain-opts">
              {DOMAINS.map((d) => (
                <button
                  type="button"
                  key={d.code}
                  className={`opt${custom.has(d.code) ? " on" : ""}`}
                  aria-pressed={custom.has(d.code)}
                  onClick={() => toggleDomain(d.code)}
                >
                  <span className="opt-t">{d.label}</span>
                  <span className="opt-d">{d.desc}</span>
                </button>
              ))}
            </div>
          )}
        </>
      ) : (
        <p className="reads-summary agent-nodata">{currentAgent.note}</p>
      )}

      {withTitle && (
        <input
          className="session-title"
          aria-label="Session title"
          placeholder="Title (optional — auto-titled later)"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      )}

      {showScope && (
        <p className="writes-note">
          Reads only. Anything the agent wants to change is <b>staged as a Proposal</b> for your
          okay.
        </p>
      )}
    </Sheet>
  );
}
