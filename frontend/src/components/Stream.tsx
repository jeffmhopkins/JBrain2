// Entry-mode home stream (docs/reference/DESIGN.md "Home stream"): bounded to the last
// 2 days (older notes live in Search), 3-line bubble clamp, ingest-state
// chips, and the swipe-left action rail (Delete / Edit / Hide). Move domain
// lives in the note-view ⋯ menu, not the rail.

import { type TouchEvent, useEffect, useRef, useState } from "react";
import { attachmentUrl } from "../api/client";
import { groupByDay, isWithinLastDays, relativeTime } from "../notes/grouping";
import { type LifecycleSource, lifecycleChip } from "../notes/lifecycle";
import { DOMAIN_COLOR, DOMAIN_LABEL } from "../notes/modes";
import { parseNote, previewText } from "../notes/noteBlocks";
import { type Drag, RAIL_WIDTH, beginDrag, endDrag, moveDrag } from "../notes/swipe";
import type { NoteThread, NoteThreads } from "../notes/useNoteThreads";
import type { StreamItem } from "../notes/useNotes";
import { ClipIcon, EyeOffIcon, PencilIcon, TrashIcon } from "./icons";

const STREAM_DAYS = 2;

function headText(item: StreamItem): string {
  const time = item.pending
    ? `${relativeTime(item.createdAt)} · pending`
    : relativeTime(item.createdAt);
  const domainLabel = DOMAIN_LABEL[item.domain];
  if (domainLabel && item.destination) return `${time} · ${domainLabel} → ${item.destination}`;
  if (domainLabel) return `${time} · ${domainLabel}`;
  return time;
}

/** Pipeline lifecycle chip: indexing… → reading image(s)… → analyzing…,
 * rose on failure, nothing once analysis is done (see notes/lifecycle.ts). */
export function IngestChip({ item }: { item: LifecycleSource }) {
  const chip = lifecycleChip(item);
  if (chip === null) return null;
  return <span className={`chip chip-${chip.tone}`}>{chip.label}</span>;
}

/** The waiting-thread chip (AGENT_INGEST_REWRITE §3b I1): a note whose conversation is
 * parked on an answer carries `N questions` and NOTHING ELSE — no answer control, no
 * candidate, no verb. The row is a redirect, which is the same ruling `NotesInboxEntry`
 * enforces on the wire one surface over.
 *
 * Amber, not the mock's rose: rose is the MEDICAL domain in this palette (it is the hue
 * of this very row's own dot), so a rose chip says the same thing twice on a medical note
 * and something false on a financial one. Amber is the open-ask register the pending
 * lifecycle chips and the inbox's ask chip already use — and the colour is not the only
 * carrier, the words are.
 *
 * ⟲ **It opens the NOTE SCREEN, the same place the row's own tap goes** — not home's
 * conversation surface, which is what I2 (ii) wired and what the owner reversed on
 * 2026-09-14: *"When I go to do a follow-up, it shouldn't open in the brain chat. It
 * should open up right there in the note entry chat."* The note screen now opens ON its
 * conversation, so there is exactly one destination and the chip's job is no longer to be
 * a second door — it is the label that says why to walk through this one. It stays a
 * 44px button rather than reverting to a `<span>`: it shares a WRAPPING row with the
 * attachment links, where an out-of-flow hit area takes a neighbour's tap
 * (`backend/tests/unit/test_tap_targets.py`), and a generous target on a dense day is
 * worth more than the height it costs. */
function AskChip({ thread, onOpen }: { thread: NoteThread; onOpen: () => void }) {
  const n = thread.questions;
  return (
    <button type="button" className="chip chip-pending chip-ask" onClick={onOpen}>
      {n === 0 ? "waiting on you" : `${n} question${n === 1 ? "" : "s"}`}
    </button>
  );
}

interface NoteRowProps {
  item: StreamItem;
  railOpen: boolean;
  onRailChange: (open: boolean) => void;
  onOpen: (item: StreamItem) => void;
  onEdit: (item: StreamItem) => void;
  onDelete: (id: string) => void;
  onHide: (item: StreamItem) => void;
  /** This note's conversation, when it is waiting on an answer — the chip's state. */
  thread?: NoteThread | undefined;
}

function NoteRow({
  item,
  railOpen,
  onRailChange,
  onOpen,
  onEdit,
  onDelete,
  onHide,
  thread,
}: NoteRowProps) {
  const [drag, setDrag] = useState<Drag | null>(null);
  const [confirming, setConfirming] = useState(false);
  const dragged = useRef(false);
  const bodyRef = useRef<HTMLSpanElement>(null);
  const added = parseNote(item.body).blocks.length;
  const [clamped, setClamped] = useState(false);

  // Truncation affordance: only show "more" when the clamp actually cut text.
  useEffect(() => {
    const el = bodyRef.current;
    if (el) setClamped(el.scrollHeight > el.clientHeight + 1);
  }, []);

  useEffect(() => {
    if (!railOpen) setConfirming(false);
  }, [railOpen]);

  // Outbox-only rows have no server id yet — nothing to PATCH or DELETE.
  const canSwipe = item.id !== null;
  const dragging = drag !== null && drag.axis === "h";
  const offset = dragging ? drag.offset : railOpen ? -RAIL_WIDTH : 0;

  function onTouchStart(event: TouchEvent) {
    if (!canSwipe) return;
    dragged.current = false;
    const t = event.touches[0];
    if (t) setDrag(beginDrag(t.clientX, t.clientY, railOpen));
  }

  function onTouchMove(event: TouchEvent) {
    if (drag === null) return;
    const t = event.touches[0];
    if (!t) return;
    const next = moveDrag(drag, t.clientX, t.clientY);
    if (next.axis === "v") {
      // Vertical dominance: hand the gesture back to list scrolling.
      setDrag(null);
      return;
    }
    setDrag(next);
  }

  function onTouchEnd() {
    if (drag === null) return;
    if (drag.axis === "h") {
      dragged.current = true;
      onRailChange(endDrag(drag));
    }
    setDrag(null);
  }

  function onBubbleTap() {
    if (dragged.current) {
      dragged.current = false;
      return;
    }
    if (railOpen) {
      onRailChange(false);
      return;
    }
    onOpen(item);
  }

  return (
    <div className="note-wrap">
      {canSwipe && offset < 0 && (
        <div className="note-rail">
          <button
            type="button"
            className={`rail-btn rail-delete${confirming ? " rail-armed" : ""}`}
            onClick={() => {
              if (!confirming) {
                setConfirming(true);
                return;
              }
              onRailChange(false);
              if (item.id !== null) onDelete(item.id);
            }}
          >
            {confirming ? (
              "tap again"
            ) : (
              <>
                <TrashIcon size={19} />
                delete
              </>
            )}
          </button>
          <button
            type="button"
            className="rail-btn rail-edit"
            onClick={() => {
              onRailChange(false);
              onEdit(item);
            }}
          >
            <PencilIcon size={19} />
            edit
          </button>
          <button
            type="button"
            className="rail-btn rail-hide"
            onClick={() => {
              onRailChange(false);
              onHide(item);
            }}
          >
            <EyeOffIcon size={19} />
            hide
          </button>
        </div>
      )}
      <div
        className={`note note-slide${dragging ? " note-dragging" : ""}`}
        style={{ transform: `translateX(${offset}px)` }}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        {/* Attachment links can't nest inside a button, so only the head/body
            area is the tap target; the chips row keeps its own links. */}
        <button type="button" className="note-tap" onClick={onBubbleTap}>
          <span className="note-head">
            <span
              className="domain-dot"
              style={{ background: DOMAIN_COLOR[item.domain] ?? "var(--steel)" }}
            />
            {headText(item)}
            {item.provenance === "agent" && (
              // Attribution is metadata, surfaced as a quiet tag — never written
              // into the note body (docs/reference/ASSISTANT.md #7).
              <span className="note-by-assistant"> · assistant</span>
            )}
          </span>
          {/* The WORDS, never the machine syntax. A composed note carries dated
              block markers in its text (`notes/compose.py`), and rendering that
              verbatim spent both lines of a clamped row on
              `[addition 2026-09-15 01:24 UTC]` and cut off before the sentence he
              had actually typed. The stamps are not lost — the note screen dates
              every block — they are just not what a two-line preview is for. */}
          <span className="note-body note-body-clamp" ref={bodyRef}>
            {previewText(item.body)}
          </span>
          {clamped && <span className="note-more">more</span>}
          {added > 0 && (
            // Quiet, and counted rather than hidden: the preview now reads as one
            // continuous note, so without this there is nothing saying he came back
            // to it — which is the thing a stamp was badly doing.
            <span className="note-added">{added} added since</span>
          )}
        </button>
        {(item.attachments.length > 0 ||
          item.pending ||
          thread !== undefined ||
          lifecycleChip(item) !== null) && (
          <div className="note-chips">
            {item.attachments.map((att) =>
              att.id ? (
                <a
                  key={`${item.key}-${att.id}`}
                  className="chip"
                  href={attachmentUrl(att.id)}
                  target="_blank"
                  rel="noreferrer"
                >
                  <ClipIcon size={12} /> {att.filename}
                </a>
              ) : (
                <span key={`${item.key}-${att.filename}`} className="chip">
                  <ClipIcon size={12} /> {att.filename}
                </span>
              ),
            )}
            {item.pending && <span className="chip chip-pending">pending sync</span>}
            {/* A waiting thread outranks the lifecycle chip: the pass that asked has
                stopped, so "analyzing…" is no longer what is happening, and two chips on
                one row would be the loudest thing in the stream. A SETTLED note wears no
                chip at all — `lifecycle.ts` makes "analyzed" the quiet end state, and only
                the waiting state earns one. */}
            {!item.pending &&
              (thread !== undefined ? (
                // Through the row's own tap handler so a chip tap behaves like a row tap
                // — it closes an open swipe rail instead of navigating out from under it.
                <AskChip thread={thread} onOpen={onBubbleTap} />
              ) : (
                <IngestChip item={item} />
              ))}
          </div>
        )}
      </div>
    </div>
  );
}

interface StreamProps {
  items: StreamItem[];
  onOpenSearch: () => void;
  onOpenNote: (item: StreamItem) => void;
  onEdit: (item: StreamItem) => void;
  onDelete: (id: string) => void;
  onHide: (item: StreamItem) => void;
  /** Note conversations parked on an answer, by note id — the chip's state (§3b I1).
   * Absent/empty = no chip is offered. */
  threads?: NoteThreads | undefined;
}

export function Stream({
  items,
  onOpenSearch,
  onOpenNote,
  onEdit,
  onDelete,
  onHide,
  threads,
}: StreamProps) {
  const scrollerRef = useRef<HTMLElement>(null);
  // One rail open at a time, like every messaging app.
  const [openRailKey, setOpenRailKey] = useState<string | null>(null);

  const recent = items.filter((item) => isWithinLastDays(item.createdAt, STREAM_DAYS));

  // New rows land at the bottom; keep the latest in view like a chat log.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run per append; the effect reads the DOM, not the items.
  useEffect(() => {
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [recent.length]);

  const groups = groupByDay(recent, (item) => item.createdAt);

  return (
    <main className="stream" ref={scrollerRef}>
      <div className="stream-inner">
        {items.length > 0 && (
          <button type="button" className="older-pill" onClick={onOpenSearch}>
            older notes live in Search ↑
          </button>
        )}
        {items.length === 0 && (
          <p className="stream-empty">Nothing captured yet — write your first entry below.</p>
        )}
        {items.length > 0 && recent.length === 0 && (
          <p className="stream-empty">nothing captured in the last two days.</p>
        )}
        {groups.map((group) => (
          <section key={group.key}>
            <h2 className="day-header">{group.label}</h2>
            <div className="day-card">
              {group.items.map((item) => (
                <NoteRow
                  key={item.key}
                  item={item}
                  railOpen={openRailKey === item.key}
                  onRailChange={(open) => setOpenRailKey(open ? item.key : null)}
                  onOpen={onOpenNote}
                  onEdit={onEdit}
                  onDelete={onDelete}
                  onHide={onHide}
                  thread={item.id === null ? undefined : threads?.get(item.id)}
                />
              ))}
            </div>
          </section>
        ))}
      </div>
    </main>
  );
}
