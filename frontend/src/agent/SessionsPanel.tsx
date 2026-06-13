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

// Group ended chats by recency off last_active_at; the live chat floats to its own
// section so "you are here" reads at a glance.
const startOfDay = (d: Date): number =>
  new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
function dayBucket(iso: string, now: Date): "Today" | "Yesterday" | "Earlier" {
  const days = Math.round((startOfDay(now) - startOfDay(new Date(iso))) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  return "Earlier";
}

// Above this many chats, the search field earns its place; below it, it's clutter.
const SEARCH_THRESHOLD = 6;

interface Props {
  sessions: AgentSession[];
  /** The chat currently open in the surface, marked "you are here". */
  activeId?: string | null;
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
  activeId,
  onOpen,
  onCreate,
  onClose,
  onRename,
  onDelete,
  onArchive,
  onUnarchive,
  onRescope,
}: Props): ReactNode {
  const [picking, setPicking] = useState(false);
  const [query, setQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  // The chat whose scope is being edited (its chip was tapped), or null.
  const [rescoping, setRescoping] = useState<AgentSession | null>(null);
  // One swipe rail open at a time (like the home stream).
  const [railId, setRailId] = useState<string | null>(null);

  const q = query.trim().toLowerCase();
  const matches = (s: AgentSession): boolean =>
    q === "" || (s.title || "untitled chat").toLowerCase().includes(q);
  // Archived chats are tucked behind a toggle; the live list never shows them.
  const live = sessions.filter((s) => s.status !== "archived" && matches(s));
  const archived = sessions.filter((s) => s.status === "archived" && matches(s));

  // Newest-active first, then bucket by recency.
  const ordered = [...live].sort(
    (a, b) => Date.parse(b.last_active_at) - Date.parse(a.last_active_at),
  );
  const now = new Date();
  const buckets: { label: string; rows: AgentSession[] }[] = [
    { label: "Today", rows: [] },
    { label: "Yesterday", rows: [] },
    { label: "Earlier", rows: [] },
  ];
  for (const s of ordered) {
    const b = buckets.find((x) => x.label === dayBucket(s.last_active_at, now));
    b?.rows.push(s);
  }

  const row = (s: AgentSession): ReactNode => (
    <SessionRow
      key={s.id}
      session={s}
      active={s.id === activeId}
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

  return (
    <section className="panel-content" aria-label="Chats">
      <div className="panel-bar">
        <button type="button" className="back" aria-label="Back to chat" onClick={onClose}>
          ‹
        </button>
        <span className="ttl">Chats</span>
        <span className="sub">tap one, or start fresh</span>
      </div>
      <div className="panel-body">
        <button type="button" className="row new-session" onClick={() => setPicking(true)}>
          ＋ New chat
        </button>

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

        {buckets.map((b) =>
          b.rows.length > 0 ? (
            <div key={b.label}>
              <div className="sect">{b.label}</div>
              {b.rows.map(row)}
            </div>
          ) : null,
        )}

        {archived.length > 0 && (
          <>
            <button
              type="button"
              className="archived-toggle"
              aria-expanded={showArchived}
              onClick={() => setShowArchived((v) => !v)}
            >
              {showArchived ? "Hide" : "Show"} {archived.length} archived
            </button>
            {showArchived && (
              <div>
                <div className="sect">Archived</div>
                {archived.map(row)}
              </div>
            )}
          </>
        )}

        {sessions.length === 0 && (
          <div className="panel-empty">No chats yet — start one to ask about your brain.</div>
        )}
        {sessions.length > 0 && live.length === 0 && archived.length === 0 && (
          <div className="panel-empty">No chats match “{query.trim()}”.</div>
        )}
      </div>

      {picking && (
        <ScopeSheet
          sheetTitle="New chat"
          lead="Picks up where you left off — reads what your last chat read. Choose a preset to change it; you can always narrow."
          seed={readLastScope()}
          actionLabel="Start"
          withTitle
          onClose={() => setPicking(false)}
          onSubmit={async (scope, title) => {
            const created = await onCreate({ domain_scopes: scope, title });
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
        <div className="session-rail rail-3">
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
        className={`row session-slide${active ? " live" : ""}`}
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
          <>
            <button
              type="button"
              className="session-tap"
              onClick={onTap}
              aria-current={active ? "true" : undefined}
            >
              <div className="r-head">
                {active && <span className="live-dot" aria-hidden="true" />}
                {session.title || "Untitled chat"}
              </div>
              {session.preview ? <div className="r-sub">{session.preview}</div> : null}
            </button>
            <div className="c-foot">
              {/* The scope chip is now a control: tap to re-scope after start. */}
              <button
                type="button"
                className={`scope-chip ${scope.cls}`}
                onClick={(e) => {
                  e.stopPropagation();
                  if (railOpen) {
                    onRailChange(false);
                    return;
                  }
                  onEditScope(session);
                }}
              >
                reads {scope.label}
              </button>
              {session.turn_count ? (
                <span className="r-turns">
                  {session.turn_count} turn{session.turn_count === 1 ? "" : "s"}
                </span>
              ) : null}
              {session.staged_count ? (
                <span className="stat staged">{session.staged_count} staged</span>
              ) : null}
            </div>
          </>
        )}
      </div>
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

// One sheet for both flows: starting a new chat and re-scoping an existing one.
// Read scope is the same dial in both — presets up front, the per-domain grid
// behind Custom — differing only in the verb and whether a title field shows.
function ScopeSheet({
  sheetTitle,
  lead,
  seed,
  actionLabel,
  withTitle = false,
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
  onClose: () => void;
  onSubmit: (scope: string[], title: string) => void | Promise<void>;
}): ReactNode {
  const [preset, setPreset] = useState<string>(() => scopeToPreset(seed));
  const [custom, setCustom] = useState<Set<string>>(() => new Set(seed));
  const [title, setTitle] = useState("");

  const scope =
    preset === "custom" ? [...custom] : (PRESETS.find((p) => p.id === preset)?.set ?? []);
  const summary = scopeKind(scope);

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
    if (scope.length === 0) return;
    writeLastScope(scope); // the chosen scope becomes the next chat's default
    void onSubmit(scope, title.trim());
  }

  return (
    <Sheet title={sheetTitle} onClose={onClose}>
      <p className="lead">{lead}</p>

      <button type="button" className="start-big" disabled={scope.length === 0} onClick={submit}>
        <span className="start-main">{actionLabel}</span>
        <span className="start-hint">
          reads {scope.length === 0 ? "nothing yet" : summary.label}
        </span>
      </button>

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

      {withTitle && (
        <input
          className="session-title"
          aria-label="Session title"
          placeholder="Title (optional — auto-titled later)"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      )}

      <p className="writes-note">
        Reads only. Anything the agent wants to change is <b>staged as a Proposal</b> for your okay.
      </p>
    </Sheet>
  );
}
