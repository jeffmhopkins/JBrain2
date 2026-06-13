// A list's checklist — the detail layer over the Lists grid (docs/mocks/
// lists-home-card.html). A slide-up layer like the note/entity views: back
// chevron + swipe-down exit. The whole row toggles an item; Edit mode reveals
// reorder (drag the ≡ handle), inline rename, per-item delete, plus rename/
// delete the list. Every action writes directly (lists are the owner's own data).

import {
  type PointerEvent as ReactPointerEvent,
  type TouchEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import { type ListItemOut, type ListOut, api } from "../api/client";
import { TopBar } from "../components/TopBar";
import { TrashIcon } from "../components/icons";
import { DOMAIN_COLOR } from "../notes/modes";
import type { SyncStatus } from "../notes/useNotes";

const SWIPE_DOWN_PX = 56;

type State = { phase: "loading" } | { phase: "error" } | { phase: "done" };

interface ListDetailScreenProps {
  listId: string;
  syncStatus: SyncStatus;
  onClose: () => void;
}

/** Move `id` to sit before `beforeId` (or to the end when null), returning a new
 * array. Pure so the drag reorder is testable without a layout engine. */
export function reorderItems(
  items: ListItemOut[],
  id: string,
  beforeId: string | null,
): ListItemOut[] {
  const moving = items.find((i) => i.id === id);
  if (!moving) return items;
  const rest = items.filter((i) => i.id !== id);
  const at = beforeId === null ? rest.length : rest.findIndex((i) => i.id === beforeId);
  rest.splice(at < 0 ? rest.length : at, 0, moving);
  return rest;
}

export function ListDetailScreen({ listId, syncStatus, onClose }: ListDetailScreenProps) {
  const [state, setState] = useState<State>({ phase: "loading" });
  const [domain, setDomain] = useState("general");
  const [title, setTitle] = useState("");
  const [items, setItems] = useState<ListItemOut[]>([]);
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState("");
  const scrollerRef = useRef<HTMLDivElement>(null);
  const swipeStart = useRef<{ x: number; y: number } | null>(null);
  const dragId = useRef<string | null>(null);

  useEffect(() => {
    let stale = false;
    setState({ phase: "loading" });
    api
      .getList(listId)
      .then((l: ListOut) => {
        if (stale) return;
        setDomain(l.domain);
        setTitle(l.title);
        setItems(l.items);
        setState({ phase: "done" });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [listId]);

  // --- swipe-down at scroll-top climbs back, like every card layer. A drag
  // reorder or a focused input opts out so the gestures don't fight. ---
  function onTouchStart(event: TouchEvent): void {
    const target = event.target as HTMLElement;
    if (dragId.current || target.closest("input, .ld-grip")) {
      swipeStart.current = null;
      return;
    }
    if ((scrollerRef.current?.scrollTop ?? 0) > 4) {
      swipeStart.current = null;
      return;
    }
    const t = event.touches[0];
    swipeStart.current = t ? { x: t.clientX, y: t.clientY } : null;
  }
  function onTouchMove(event: TouchEvent): void {
    const start = swipeStart.current;
    const t = event.touches[0];
    if (!start || !t) return;
    const dy = t.clientY - start.y;
    if (dy > SWIPE_DOWN_PX && dy > Math.abs(t.clientX - start.x) * 2) {
      swipeStart.current = null;
      onClose();
    }
  }

  function toggle(item: ListItemOut): void {
    const next = !item.checked;
    setItems((xs) => xs.map((x) => (x.id === item.id ? { ...x, checked: next } : x)));
    api.setListItemChecked(item.id, next).catch(() => {
      setItems((xs) => xs.map((x) => (x.id === item.id ? { ...x, checked: item.checked } : x)));
    });
  }

  function renameItem(item: ListItemOut, body: string): void {
    const trimmed = body.trim();
    if (!trimmed || trimmed === item.body) return;
    setItems((xs) => xs.map((x) => (x.id === item.id ? { ...x, body: trimmed } : x)));
    void api.renameListItem(item.id, trimmed).catch(() => {});
  }

  function removeItem(item: ListItemOut): void {
    setItems((xs) => xs.filter((x) => x.id !== item.id));
    void api.removeListItem(item.id).catch(() => {});
  }

  function addItem(): void {
    const body = adding.trim();
    if (!body) return;
    setAdding("");
    api
      .addListItem(listId, body)
      .then((made) => setItems((xs) => [...xs, made]))
      .catch(() => {});
  }

  function commitTitle(): void {
    const t = title.trim();
    if (t) void api.renameList(listId, t).catch(() => {});
  }

  function deleteList(): void {
    void api.deleteList(listId).catch(() => {});
    onClose();
  }

  // --- drag reorder (the ≡ handle) ---
  function onGripDown(item: ListItemOut, e: ReactPointerEvent): void {
    dragId.current = item.id;
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
  }
  function onGripMove(e: ReactPointerEvent): void {
    const id = dragId.current;
    if (!id) return;
    const rows = [...(scrollerRef.current?.querySelectorAll<HTMLElement>(".ld-row") ?? [])];
    const before = rows.find(
      (r) => r.dataset.id !== id && e.clientY < r.getBoundingClientRect().top + r.offsetHeight / 2,
    );
    setItems((xs) => reorderItems(xs, id, before?.dataset.id ?? null));
  }
  function onGripUp(): void {
    if (!dragId.current) return;
    dragId.current = null;
    void api
      .reorderListItems(
        listId,
        items.map((i) => i.id),
      )
      .catch(() => {});
  }

  return (
    <div className="subscreen subscreen-list" onTouchStart={onTouchStart} onTouchMove={onTouchMove}>
      <TopBar title="List" onBack={onClose} syncStatus={syncStatus} onBolt={onClose} />
      <div className="screen-body list-detail" ref={scrollerRef}>
        {state.phase === "loading" && <p className="analysis-quiet">loading list…</p>}
        {state.phase === "error" && (
          <p className="analysis-quiet">couldn't load this list — reopen to retry.</p>
        )}
        {state.phase === "done" && (
          <>
            <div className="ld-head">
              <span
                className="dot"
                style={{ background: DOMAIN_COLOR[domain] ?? "var(--steel)" }}
              />
              {editing ? (
                <input
                  className="ld-title"
                  aria-label="List title"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  onBlur={commitTitle}
                  onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
                />
              ) : (
                <h2 className="ld-title">{title}</h2>
              )}
              <button type="button" className="ld-edit" onClick={() => setEditing((v) => !v)}>
                {editing ? "Done" : "Edit"}
              </button>
            </div>

            <ul className={`ld-items${editing ? " editing" : ""}`}>
              {items.map((it) =>
                editing ? (
                  <li key={it.id} className="ld-row" data-id={it.id}>
                    <button
                      type="button"
                      className="ld-grip"
                      aria-label="Reorder"
                      onPointerDown={(e) => onGripDown(it, e)}
                      onPointerMove={onGripMove}
                      onPointerUp={onGripUp}
                      onPointerCancel={onGripUp}
                    >
                      <GripGlyph />
                    </button>
                    <input
                      className="ld-body-edit"
                      aria-label={`Edit ${it.body}`}
                      defaultValue={it.body}
                      onBlur={(e) => renameItem(it, e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
                    />
                    <button
                      type="button"
                      className="ld-del"
                      aria-label={`Delete ${it.body}`}
                      onClick={() => removeItem(it)}
                    >
                      <TrashIcon size={15} />
                    </button>
                  </li>
                ) : (
                  <li key={it.id} className="ld-row" data-id={it.id}>
                    <button
                      type="button"
                      className={`ld-toggle${it.checked ? " checked" : ""}`}
                      aria-pressed={it.checked}
                      onClick={() => toggle(it)}
                    >
                      <span className="ld-box" aria-hidden="true" />
                      <span className="ld-body">{it.body}</span>
                    </button>
                  </li>
                ),
              )}
            </ul>

            <div className="ld-add">
              <span className="ld-add-plus" aria-hidden="true">
                ＋
              </span>
              <input
                aria-label="Add item"
                placeholder="add item"
                value={adding}
                onChange={(e) => setAdding(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && addItem()}
              />
            </div>

            {editing && (
              <button type="button" className="ld-delete-list" onClick={deleteList}>
                Delete list
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// Self-sized via attributes (the app's icon convention) — a CSS-only-sized bare
// <svg> didn't render reliably on the device.
function GripGlyph() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M8 6h.01M8 12h.01M8 18h.01M16 6h.01M16 12h.01M16 18h.01" />
    </svg>
  );
}
