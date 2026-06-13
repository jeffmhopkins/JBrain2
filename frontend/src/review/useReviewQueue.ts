// Review-inbox controller for the split-inbox redesign. Loads three lanes —
// pending (open), deferred (parked), and decided (the resolved log) — and
// moves items between them optimistically: the UI moves immediately, a failed
// POST rolls the move back (the useNotes optimistic pattern).
//
// Decisions are real graph writes, so "undo" is the server's own unwind: it
// reopens what was just decided/parked. Un-parking a deferred item is clean;
// undoing a hard decision unwinds it and leaves a reopened tombstone — honest,
// because the write really happened.

import { useCallback, useEffect, useRef, useState } from "react";
import { type BatchDecision, type ReviewItem, api } from "../api/client";

/** Actions that park an item in the deferred lane instead of deciding it. */
const DEFER_ACTIONS = new Set(["defer", "discuss"]);

export interface PendingUndo {
  ids: string[];
  label: string;
}

export interface ReviewQueueController {
  /** null until the lane's first load resolves. */
  pending: ReviewItem[] | null;
  deferred: ReviewItem[] | null;
  decided: ReviewItem[] | null;
  loadError: boolean;
  /** Failed action message; cleared on the next action. */
  actionError: string | null;
  /** Set after a decision/defer/batch; drives the undo snackbar. */
  undoable: PendingUndo | null;
  resolve(id: string, action: string, payload?: Record<string, unknown>): void;
  /** File the human's fix as a correction note (the #7 channel), then resolve
   * the item as corrected. The graph change is the pipeline's, when it
   * processes the note. */
  correct(id: string, body: string): void;
  batch(decisions: BatchDecision[], label: string): void;
  reopen(id: string): void;
  undo(): void;
  dismissUndo(): void;
}

type LaneSetter = React.Dispatch<React.SetStateAction<ReviewItem[] | null>>;

export function useReviewQueue(): ReviewQueueController {
  const [pending, setPending] = useState<ReviewItem[] | null>(null);
  const [deferred, setDeferred] = useState<ReviewItem[] | null>(null);
  const [decided, setDecided] = useState<ReviewItem[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [undoable, setUndoable] = useState<PendingUndo | null>(null);

  // Mirrors for the callbacks: read here, never mutate, so each POST fires
  // once under StrictMode.
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const deferredRef = useRef(deferred);
  deferredRef.current = deferred;
  const decidedRef = useRef(decided);
  decidedRef.current = decided;

  useEffect(() => {
    let stale = false;
    const load = (status: "open" | "deferred" | "resolved", set: LaneSetter, onErr?: () => void) =>
      api
        .reviewQueue(status)
        .then((q) => {
          if (!stale) set(q.items);
        })
        .catch(() => {
          if (!stale) onErr?.();
        });
    load("open", setPending, () => setLoadError(true));
    load("deferred", setDeferred);
    load("resolved", setDecided);
    return () => {
      stale = true;
    };
  }, []);

  /** Remove ids from a lane; returns the removed items in request order. */
  const take = useCallback((set: LaneSetter, ids: string[]): void => {
    set((prev) => (prev === null ? prev : prev.filter((r) => !ids.includes(r.id))));
  }, []);

  const resolve = useCallback(
    (id: string, action: string, payload: Record<string, unknown> = {}) => {
      const current = pendingRef.current?.find((r) => r.id === id);
      if (current === undefined) return;
      setActionError(null);
      const parked = DEFER_ACTIONS.has(action);
      const optimistic: ReviewItem = {
        ...current,
        status: parked ? "deferred" : action === "dismiss" ? "dismissed" : "resolved",
        resolution: { action, payload },
        resolved_at: new Date().toISOString(),
      };
      const target: LaneSetter = parked ? setDeferred : setDecided;
      take(setPending, [id]);
      target((prev) => (prev === null ? [optimistic] : [optimistic, ...prev]));
      setUndoable({ ids: [id], label: parked ? "Parked" : "Decided" });
      api
        .reviewResolve(id, action, payload)
        .then((server) => {
          target((now) => (now === null ? now : now.map((r) => (r.id === id ? server : r))));
        })
        .catch(() => {
          target((now) => (now === null ? now : now.filter((r) => r.id !== id)));
          setPending((now) => (now === null ? [current] : [current, ...now]));
          setUndoable(null);
          setActionError("couldn't save that decision — try again.");
        });
    },
    [take],
  );

  const correct = useCallback(
    (id: string, body: string) => {
      const current = pendingRef.current?.find((r) => r.id === id);
      if (current === undefined) return;
      setActionError(null);
      // File the correction as a real note in the item's domain; on success,
      // resolve the item as corrected and link it. resolve() does the
      // optimistic move once the note id is in hand.
      api
        .createNote({ client_id: crypto.randomUUID(), domain: current.domain, body })
        .then((note) => resolve(id, "correct", { note_id: note.id, summary: body.slice(0, 140) }))
        .catch(() => setActionError("couldn't file the correction — try again."));
    },
    [resolve],
  );

  const batch = useCallback(
    (decisions: BatchDecision[], label: string) => {
      const byId = new Map((pendingRef.current ?? []).map((r) => [r.id, r]));
      const moving = decisions.filter((d) => byId.has(d.id));
      if (moving.length === 0) return;
      setActionError(null);
      const optimistic = moving.map((d): { item: ReviewItem; parked: boolean } => {
        const src = byId.get(d.id) as ReviewItem;
        const parked = DEFER_ACTIONS.has(d.action);
        return {
          parked,
          item: {
            ...src,
            status: parked ? "deferred" : d.action === "dismiss" ? "dismissed" : "resolved",
            resolution: { action: d.action, payload: d.payload ?? {} },
            resolved_at: new Date().toISOString(),
          },
        };
      });
      take(
        setPending,
        moving.map((d) => d.id),
      );
      const parkedItems = optimistic.filter((o) => o.parked).map((o) => o.item);
      const decidedItems = optimistic.filter((o) => !o.parked).map((o) => o.item);
      if (parkedItems.length > 0)
        setDeferred((prev) => (prev === null ? parkedItems : [...parkedItems, ...prev]));
      if (decidedItems.length > 0)
        setDecided((prev) => (prev === null ? decidedItems : [...decidedItems, ...prev]));
      setUndoable({ ids: moving.map((d) => d.id), label });
      api
        .reviewResolveBatch(moving)
        .then((res) => {
          const okIds = new Set(res.items.map((r) => r.id));
          // Swap server records in; roll any failed ids back to pending.
          const failed = moving.filter((d) => !okIds.has(d.id)).map((d) => d.id);
          const fix = (now: ReviewItem[] | null) =>
            now === null
              ? now
              : now
                  .map((r) => res.items.find((s) => s.id === r.id) ?? r)
                  .filter((r) => !failed.includes(r.id));
          setDeferred(fix);
          setDecided(fix);
          if (failed.length > 0) {
            const back = moving
              .filter((d) => failed.includes(d.id))
              .map((d) => byId.get(d.id) as ReviewItem);
            setPending((now) => (now === null ? back : [...back, ...now]));
            setActionError(`${failed.length} couldn't be saved — left in pending.`);
          }
        })
        .catch(() => {
          const ids2 = new Set(moving.map((d) => d.id));
          setDeferred((now) => (now === null ? now : now.filter((r) => !ids2.has(r.id))));
          setDecided((now) => (now === null ? now : now.filter((r) => !ids2.has(r.id))));
          const back = moving.map((d) => byId.get(d.id) as ReviewItem);
          setPending((now) => (now === null ? back : [...back, ...now]));
          setUndoable(null);
          setActionError("couldn't save those decisions — try again.");
        });
    },
    [take],
  );

  const reopen = useCallback(
    (id: string) => {
      const record =
        deferredRef.current?.find((r) => r.id === id) ??
        decidedRef.current?.find((r) => r.id === id);
      if (record === undefined || record.status === "open") return;
      setActionError(null);
      const wasParked = record.status === "deferred";
      // Optimistic move back to pending. A parked item un-parks clean; a decided
      // one leaves a struck-through reopened tombstone in the log.
      const requeued: ReviewItem = {
        ...record,
        status: "open",
        resolved_at: null,
        resolution: null,
      };
      const tombstone: ReviewItem = {
        ...record,
        status: "open",
        resolved_at: null,
        resolution: {
          ...(record.resolution ?? { action: "dismiss", payload: {} }),
          reopened_at: new Date().toISOString(),
        },
      };
      if (wasParked) {
        take(setDeferred, [id]);
      } else {
        setDecided((prev) =>
          prev === null ? prev : prev.map((r) => (r.id === id ? tombstone : r)),
        );
      }
      setPending((prev) => (prev === null ? [requeued] : [...prev, requeued]));
      api
        .reviewReopen(id)
        .then((server) => {
          setPending((now) => (now === null ? now : now.map((r) => (r.id === id ? server : r))));
          if (!wasParked)
            setDecided((now) => (now === null ? now : now.map((r) => (r.id === id ? server : r))));
        })
        .catch(() => {
          setPending((now) => (now === null ? now : now.filter((r) => r.id !== id)));
          if (wasParked) {
            setDeferred((now) => (now === null ? [record] : [record, ...now]));
          } else {
            setDecided((now) => (now === null ? now : now.map((r) => (r.id === id ? record : r))));
          }
          setActionError("couldn't reopen that item — try again.");
        });
    },
    [take],
  );

  const undo = useCallback(() => {
    const pendingUndo = undoable;
    if (pendingUndo === null) return;
    // Undo is the server's unwind: reopen each just-decided/parked item.
    for (const id of pendingUndo.ids) reopen(id);
    setUndoable(null);
  }, [undoable, reopen]);

  const dismissUndo = useCallback(() => setUndoable(null), []);

  return {
    pending,
    deferred,
    decided,
    loadError,
    actionError,
    undoable,
    resolve,
    correct,
    batch,
    reopen,
    undo,
    dismissUndo,
  };
}
