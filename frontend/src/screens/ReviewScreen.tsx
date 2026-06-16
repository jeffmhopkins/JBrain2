// Review inbox — split-inbox redesign (docs/DESIGN.md "Review inbox"). Three
// lanes (pending · deferred · decided) behind a segmented filter. The list is
// browsable with a selection mode for bulk actions; tapping a row pushes a
// detail view with prev/next so you move between items without returning to
// the list. The detail is composed from a registry of typed blocks
// (docs/DESIGN.md "Detail composition") declared per kind in
// review/blocks/registry — header, claim:{inference,diff,notice}, trace,
// action, evidence, plus a lane-driven footer — so a new kind is a sequence,
// not a screen branch. Every decision raises an undo snackbar (undo is the
// server's own unwind). Decided rows reopen; deferred rows resume.

import { type TouchEvent, useEffect, useMemo, useRef, useState } from "react";
import { edgePath, valueLabel } from "../analysis/format";
import type { ReviewFilter, ReviewItem } from "../api/client";
import { EntityTypeIcon } from "../entities/kinds";
import { DomainDot } from "../review/DomainDot";
import { Footer } from "../review/blocks/Footer";
import { BLOCKS, blockSequenceFor } from "../review/blocks/registry";
import type { BlockCtx } from "../review/blocks/types";
import { groupByEntity } from "../review/grouping";
import {
  approveActionFor,
  confidenceBadge,
  decidedVerb,
  fmtWhen,
  kindLabel,
  parsePayload,
} from "../review/payload";
import { useArmed } from "../review/useArmed";
import { type ReviewQueueController, useReviewQueue } from "../review/useReviewQueue";

// ===== List =====

interface ListRowProps {
  item: ReviewItem;
  selectable: boolean;
  selected: boolean;
  onToggle: () => void;
  onOpen: () => void;
}

function ListRow({ item, selectable, selected, onToggle, onOpen }: ListRowProps) {
  const p = parsePayload(item.payload);
  const conf = confidenceBadge(p.confidence);
  const decided = item.status === "resolved" || item.status === "dismissed";
  const dismissed = item.status === "dismissed";
  const isDiscuss = item.resolution?.action === "discuss";
  const isInference = item.kind === "low_confidence_inference";
  return (
    <div
      className={`rrow2${dismissed ? " rrow-dismissed" : ""}${isInference ? " rrow-inference" : ""}`}
    >
      {selectable && (
        <label className="rrow-check">
          <input
            type="checkbox"
            className="rrow-cbox"
            checked={selected}
            aria-label={`select ${p.summary ?? item.kind}`}
            onChange={onToggle}
          />
        </label>
      )}
      <button type="button" className="rrow-open" onClick={onOpen}>
        <span className="rrow-line">
          <span className="kind-badge">{kindLabel(item.kind)}</span>
          <DomainDot domain={item.domain} />
          {isDiscuss && <span className="state-chip chip-discuss">with assistant</span>}
          <span className="rrow-when">{fmtWhen(item)}</span>
        </span>
        <span className="rrow-sum">{p.summary ?? item.kind}</span>
        {item.kind === "low_confidence_inference" && p.predicate !== null && (
          <span className="rrow-fact fact-edge">
            <span className="edge-path">{edgePath(p.predicate, p.qualifier)}</span>
            <span className="edge-arrow"> → </span>
            <span className="edge-value">{valueLabel(p.valueJson, p.statement ?? "")}</span>
          </span>
        )}
        <span className="rrow-meta">
          {decided ? (
            <span className={`rrow-outcome${dismissed ? " muted" : ""}`}>
              {dismissed ? "dismissed" : decidedVerb(item)}
            </span>
          ) : (
            conf && <span className={`conf-badge ${conf.cls}`}>{conf.text}</span>
          )}
        </span>
      </button>
      {!selectable && (
        <span className="rrow-chev" aria-hidden="true">
          ›
        </span>
      )}
    </div>
  );
}

// ===== Detail =====

interface DetailProps {
  item: ReviewItem;
  lane: ReviewFilter;
  queue: ReviewQueueController;
  position: { index: number; total: number } | null;
  onClose: () => void;
  // Advance to the next unresolved item in the lane, falling back to the
  // previous one, then to the list when none remain. Used after a decision so
  // triage flows item→item instead of bouncing back to the inbox.
  onAdvance: () => void;
  onNav: (delta: number) => void;
}

function Detail({ item, lane, queue, position, onClose, onAdvance, onNav }: DetailProps) {
  const parsed = parsePayload(item.payload);
  const [armed, tap] = useArmed();
  const [composing, setComposing] = useState(false);
  const [draft, setDraft] = useState("");

  // Direction C — correct in place: a low-confidence inference's predicate AND
  // value are editable on the card. The proposed-fact panel (claim:inference)
  // and the approve button (action) share this edit state, so editing either
  // side flips approve to approve correction. The predicate side is a weighted
  // picker (the canonicals nearest the proposed relation) plus free entry; the
  // value is free text, or a typed predicate's members as chips.
  const isInference = item.kind === "low_confidence_inference" && parsed.predicate !== null;
  const originalValue = isInference ? valueLabel(parsed.valueJson, parsed.statement ?? "") : "";
  const [editValue, setEditValue] = useState(originalValue);
  const [editingValue, setEditingValue] = useState(false);
  const valueEdited =
    isInference && editValue.trim().length > 0 && editValue.trim() !== originalValue;

  const originalPredicate = isInference ? (parsed.predicate ?? "") : "";
  const [editPredicate, setEditPredicate] = useState(originalPredicate);
  const [editingPredicate, setEditingPredicate] = useState(false);
  const predicateEdited =
    isInference && editPredicate.trim().length > 0 && editPredicate.trim() !== originalPredicate;

  // Carousel: swipe left/right pages to the next/prev item, the horizontal twin
  // of the ‹ › chevrons. Armed under the same condition they show, and only on a
  // horizontal-dominant drag so it never steals the vertical scroll.
  const canCarousel = lane === "pending" && position !== null && position.total > 1;
  const swipeStart = useRef<{ x: number; y: number } | null>(null);
  function onSwipeStart(event: TouchEvent) {
    const t = event.touches[0];
    swipeStart.current = t ? { x: t.clientX, y: t.clientY } : null;
  }
  function onSwipeMove(event: TouchEvent) {
    const start = swipeStart.current;
    const t = event.touches[0];
    if (!start || !t) return;
    const dx = t.clientX - start.x;
    const dy = t.clientY - start.y;
    if (Math.abs(dx) > 64 && Math.abs(dx) > Math.abs(dy) * 1.5) {
      swipeStart.current = null;
      onNav(dx < 0 ? 1 : -1); // swipe left → next, swipe right → previous
    }
  }

  const ctx: BlockCtx = {
    item,
    parsed,
    lane,
    queue,
    armed,
    tap,
    onClose,
    onAdvance,
    inference: {
      isInference,
      originalValue,
      editValue,
      setEditValue,
      editingValue,
      setEditingValue,
      valueEdited,
      originalPredicate,
      editPredicate,
      setEditPredicate,
      editingPredicate,
      setEditingPredicate,
      predicateEdited,
      predicateSuggestions: parsed.predicateSuggestions,
      edited: valueEdited || predicateEdited,
    },
    composing,
    setComposing,
    draft,
    setDraft,
  };

  return (
    <section
      className="rdetail"
      onTouchStart={canCarousel ? onSwipeStart : undefined}
      onTouchMove={canCarousel ? onSwipeMove : undefined}
    >
      <header className="rdetail-bar">
        <button type="button" className="rdetail-back" onClick={onClose}>
          ‹ inbox
        </button>
        {position && (
          <span className="rdetail-pos">
            {position.index + 1} of {position.total}
          </span>
        )}
        {lane === "pending" && position && position.total > 1 && (
          <span className="rdetail-nav">
            <button
              type="button"
              aria-label="previous"
              disabled={position.index === 0}
              onClick={() => onNav(-1)}
            >
              ‹
            </button>
            <button
              type="button"
              aria-label="next"
              disabled={position.index >= position.total - 1}
              onClick={() => onNav(1)}
            >
              ›
            </button>
          </span>
        )}
      </header>

      <div className="rdetail-scroll">
        {blockSequenceFor(item).map((id) => {
          const Block = BLOCKS[id];
          return <Block key={id} ctx={ctx} />;
        })}
        {queue.actionError !== null && <p className="review-error">{queue.actionError}</p>}
      </div>

      <Footer ctx={ctx} />
    </section>
  );
}

// ===== List view (rows, selection, bulk) =====

interface ListViewProps {
  lane: ReviewFilter;
  items: ReviewItem[] | null;
  queue: ReviewQueueController;
  onOpen: (id: string) => void;
}

function ListView({ lane, items, queue, onOpen }: ListViewProps) {
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Pending triage groups by subject entity by default; "time" is the flat,
  // chronological list. Only pending groups — deferred/decided are logs.
  const [groupMode, setGroupMode] = useState<"entity" | "time">("entity");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // Reset selection when the lane changes out from under us.
  // biome-ignore lint/correctness/useExhaustiveDependencies: lane is the trigger.
  useEffect(() => {
    setSelecting(false);
    setSelected(new Set());
    setCollapsed(new Set());
  }, [lane]);

  if (items === null) return <p className="analysis-quiet">loading…</p>;
  if (items.length === 0) return <EmptyLane lane={lane} />;

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const approvable = items.filter((i) => approveActionFor(i) !== null);
  const highConf = approvable.filter((i) => {
    const c = parsePayload(i.payload).confidence;
    return c !== null && c >= 0.75;
  });

  function bulkApprove(ids: string[]) {
    const decisions = ids
      .map((id) => {
        const item = items?.find((i) => i.id === id);
        const a = item ? approveActionFor(item) : null;
        return a ? { id, action: a.action, payload: a.payload } : null;
      })
      .filter(
        (d): d is { id: string; action: string; payload: Record<string, unknown> } => d !== null,
      );
    if (decisions.length > 0) queue.batch(decisions, `approved ${decisions.length}`);
    setSelecting(false);
    setSelected(new Set());
  }
  function bulkDefer(ids: string[]) {
    queue.batch(
      ids.map((id) => ({ id, action: "defer", payload: {} })),
      `parked ${ids.length}`,
    );
    setSelecting(false);
    setSelected(new Set());
  }

  const toggleGroup = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  const grouped = lane === "pending" && groupMode === "entity" && !selecting;
  const row = (item: ReviewItem) => (
    <ListRow
      key={item.id}
      item={item}
      selectable={selecting}
      selected={selected.has(item.id)}
      onToggle={() => toggle(item.id)}
      onOpen={() => onOpen(item.id)}
    />
  );

  return (
    <>
      {lane === "pending" && (
        <div className="rlist-tools">
          <div className="rgroup-toggle">
            <button
              type="button"
              aria-pressed={groupMode === "entity"}
              onClick={() => setGroupMode("entity")}
            >
              entity
            </button>
            <button
              type="button"
              aria-pressed={groupMode === "time"}
              onClick={() => setGroupMode("time")}
            >
              time
            </button>
          </div>
          <button type="button" className="rtool" onClick={() => setSelecting((s) => !s)}>
            {selecting ? "done" : "select"}
          </button>
          {!selecting && highConf.length >= 2 && (
            <button
              type="button"
              className="rtool rtool-suggest"
              onClick={() => bulkApprove(highConf.map((i) => i.id))}
            >
              approve {highConf.length} high-confidence
            </button>
          )}
        </div>
      )}

      {grouped ? (
        <div className="rgroups">
          {groupByEntity(items).map((g) => {
            const open = !collapsed.has(g.key);
            return (
              <div key={g.key} className={`egroup${open ? " open" : ""}`}>
                <button
                  type="button"
                  className="egroup-head"
                  aria-expanded={open}
                  onClick={() => toggleGroup(g.key)}
                >
                  <EntityTypeIcon kind={g.kind} size={32} />
                  <span className="egroup-name">{g.label}</span>
                  <span className="gcount">{g.items.length}</span>
                  <span className="gchev" aria-hidden="true">
                    ›
                  </span>
                </button>
                {open && <div className="egroup-rows">{g.items.map(row)}</div>}
              </div>
            );
          })}
        </div>
      ) : (
        <div className="rlist2">{items.map(row)}</div>
      )}

      {selecting && selected.size > 0 && (
        <div className="rbulk" role="toolbar" aria-label="bulk actions">
          <span className="rbulk-n">{selected.size} selected</span>
          <button type="button" className="rbulk-defer" onClick={() => bulkDefer([...selected])}>
            defer all
          </button>
          <button
            type="button"
            className="rbulk-approve"
            disabled={[...selected].every((id) => {
              const item = items.find((i) => i.id === id);
              return !item || approveActionFor(item) === null;
            })}
            onClick={() => bulkApprove([...selected])}
          >
            approve all
          </button>
        </div>
      )}
    </>
  );
}

function EmptyLane({ lane }: { lane: ReviewFilter }) {
  const copy: Record<ReviewFilter, string> = {
    pending: "pending is clear — new items arrive as notes are analyzed.",
    deferred: "nothing parked — items you defer or talk over collect here.",
    decided: "no decisions yet — resolved items collect here.",
  };
  return <p className="analysis-quiet rlane-empty">{copy[lane]}</p>;
}

// ===== Screen =====

export function ReviewScreen() {
  const queue = useReviewQueue();
  const [filter, setFilter] = useState<ReviewFilter>("pending");
  const [detailId, setDetailId] = useState<string | null>(null);

  const lanes: Record<ReviewFilter, ReviewItem[] | null> = {
    pending: queue.pending,
    deferred: queue.deferred,
    decided: queue.decided,
  };
  const items = lanes[filter];

  const position = useMemo(() => {
    if (detailId === null || items === null) return null;
    const index = items.findIndex((i) => i.id === detailId);
    return index < 0 ? null : { index, total: items.length };
  }, [detailId, items]);

  const detailItem = detailId !== null ? (items?.find((i) => i.id === detailId) ?? null) : null;

  // If the open item leaves the lane (decided/deferred from the detail), close.
  useEffect(() => {
    if (detailId !== null && detailItem === null) setDetailId(null);
  }, [detailId, detailItem]);

  // The undo snackbar lingers, then fades — undo stays reachable from the
  // decided/deferred lanes after it goes.
  const { undoable, dismissUndo } = queue;
  useEffect(() => {
    if (undoable === null) return;
    const t = setTimeout(dismissUndo, 7000);
    return () => clearTimeout(t);
  }, [undoable, dismissUndo]);

  function nav(delta: number) {
    if (items === null || position === null) return;
    const next = items[position.index + delta];
    if (next) setDetailId(next.id);
  }

  // After a decision the current item leaves the lane, so step to its neighbor —
  // the next item, or the previous when it was last — keeping the detail open
  // while any remain; only an emptied lane falls back to the list. Computed from
  // the pre-decision list (the optimistic removal lands in the same batch).
  function advance() {
    if (items === null || position === null) {
      setDetailId(null);
      return;
    }
    const next = items[position.index + 1] ?? items[position.index - 1] ?? null;
    setDetailId(next?.id ?? null);
  }

  const counts: Record<ReviewFilter, number | undefined> = {
    pending: queue.pending?.length,
    deferred: queue.deferred?.length,
    decided: queue.decided?.length,
  };

  return (
    <main className="screen-body review-body">
      {detailItem === null ? (
        <>
          <div className="review-segs" role="tablist">
            {(["pending", "deferred", "decided"] as ReviewFilter[]).map((f) => (
              <button
                key={f}
                type="button"
                role="tab"
                aria-selected={filter === f}
                className={`review-seg${filter === f ? " seg-active" : ""}`}
                onClick={() => setFilter(f)}
              >
                {f}
                {counts[f] !== undefined && <span className="seg-count">{counts[f]}</span>}
              </button>
            ))}
          </div>
          {queue.loadError && filter === "pending" ? (
            <p className="analysis-quiet">couldn't load the inbox — reopen to retry.</p>
          ) : (
            <ListView lane={filter} items={items} queue={queue} onOpen={setDetailId} />
          )}
        </>
      ) : (
        <Detail
          key={detailItem.id}
          item={detailItem}
          lane={filter}
          queue={queue}
          position={position}
          onClose={() => setDetailId(null)}
          onAdvance={advance}
          onNav={nav}
        />
      )}

      {queue.undoable !== null && (
        <output className="review-snack">
          <span className="snack-msg">{queue.undoable.label}</span>
          <button type="button" className="snack-undo" onClick={queue.undo}>
            undo
          </button>
          <button
            type="button"
            className="snack-x"
            aria-label="dismiss"
            onClick={queue.dismissUndo}
          >
            ✕
          </button>
        </output>
      )}
    </main>
  );
}
