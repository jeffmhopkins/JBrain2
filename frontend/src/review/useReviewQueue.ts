// Review-inbox controller for the split-inbox redesign. Loads two lanes —
// pending (open) and decided (the resolved log) — and moves items between them
// optimistically: the UI moves immediately, a failed POST rolls the move back
// (the useNotes optimistic pattern).
//
// Decisions are real graph writes, so "undo" is the server's own unwind: it
// reopens what was just decided, leaving a reopened tombstone — honest, because
// the write really happened.

import { useCallback, useEffect, useRef, useState } from "react";
import { type BatchDecision, type ReviewItem, api } from "../api/client";

export interface PendingUndo {
  ids: string[];
  label: string;
}

export interface ReviewQueueController {
  /** null until the lane's first load resolves. */
  pending: ReviewItem[] | null;
  decided: ReviewItem[] | null;
  loadError: boolean;
  /** Failed action message; cleared on the next action. */
  actionError: string | null;
  /** Set after a decision/batch; drives the undo snackbar. */
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
  const [decided, setDecided] = useState<ReviewItem[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [undoable, setUndoable] = useState<PendingUndo | null>(null);

  // Mirrors for the callbacks: read here, never mutate, so each POST fires
  // once under StrictMode.
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const decidedRef = useRef(decided);
  decidedRef.current = decided;

  useEffect(() => {
    let stale = false;
    const load = (status: "open" | "resolved", set: LaneSetter, onErr?: () => void) =>
      api
        .reviewQueue(status)
        .then((q) => {
          if (!stale) set(q.items);
        })
        .catch(() => {
          if (!stale) onErr?.();
        });
    load("open", setPending, () => setLoadError(true));
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
      const optimistic: ReviewItem = {
        ...current,
        status: action === "dismiss" ? "dismissed" : "resolved",
        resolution: { action, payload },
        resolved_at: new Date().toISOString(),
      };
      take(setPending, [id]);
      setDecided((prev) => (prev === null ? [optimistic] : [optimistic, ...prev]));
      setUndoable({ ids: [id], label: "Decided" });
      api
        .reviewResolve(id, action, payload)
        .then((server) => {
          setDecided((now) => (now === null ? now : now.map((r) => (r.id === id ? server : r))));
        })
        .catch(() => {
          setDecided((now) => (now === null ? now : now.filter((r) => r.id !== id)));
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
      // File the correction as an owner_correction note (the #7 channel) in the
      // item's domain so it force-supersedes what it corrects instead of
      // colliding with it; on success, resolve the item as corrected and link
      // it. resolve() does the optimistic move once the note id is in hand.
      api
        .reviewFileCorrection(id, current.domain, body)
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
      const optimistic = moving.map(
        (d): ReviewItem => ({
          ...(byId.get(d.id) as ReviewItem),
          status: d.action === "dismiss" ? "dismissed" : "resolved",
          resolution: { action: d.action, payload: d.payload ?? {} },
          resolved_at: new Date().toISOString(),
        }),
      );
      take(
        setPending,
        moving.map((d) => d.id),
      );
      setDecided((prev) => (prev === null ? optimistic : [...optimistic, ...prev]));
      setUndoable({ ids: moving.map((d) => d.id), label });
      api
        .reviewResolveBatch(moving)
        .then((res) => {
          const okIds = new Set(res.items.map((r) => r.id));
          // Swap server records in; roll any failed ids back to pending.
          const failed = moving.filter((d) => !okIds.has(d.id)).map((d) => d.id);
          setDecided((now) =>
            now === null
              ? now
              : now
                  .map((r) => res.items.find((s) => s.id === r.id) ?? r)
                  .filter((r) => !failed.includes(r.id)),
          );
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
          setDecided((now) => (now === null ? now : now.filter((r) => !ids2.has(r.id))));
          const back = moving.map((d) => byId.get(d.id) as ReviewItem);
          setPending((now) => (now === null ? back : [...back, ...now]));
          setUndoable(null);
          setActionError("couldn't save those decisions — try again.");
        });
    },
    [take],
  );

  const reopen = useCallback((id: string) => {
    const record = decidedRef.current?.find((r) => r.id === id);
    if (record === undefined || record.status === "open") return;
    setActionError(null);
    // Optimistic move back to pending, leaving a struck-through reopened
    // tombstone in the decided log (the write really happened).
    const requeued: ReviewItem = { ...record, status: "open", resolved_at: null, resolution: null };
    const tombstone: ReviewItem = {
      ...record,
      status: "open",
      resolved_at: null,
      resolution: {
        ...(record.resolution ?? { action: "dismiss", payload: {} }),
        reopened_at: new Date().toISOString(),
      },
    };
    setDecided((prev) => (prev === null ? prev : prev.map((r) => (r.id === id ? tombstone : r))));
    setPending((prev) => (prev === null ? [requeued] : [...prev, requeued]));
    api
      .reviewReopen(id)
      .then((server) => {
        setPending((now) => (now === null ? now : now.map((r) => (r.id === id ? server : r))));
        setDecided((now) => (now === null ? now : now.map((r) => (r.id === id ? server : r))));
      })
      .catch(() => {
        setPending((now) => (now === null ? now : now.filter((r) => r.id !== id)));
        setDecided((now) => (now === null ? now : now.map((r) => (r.id === id ? record : r))));
        setActionError("couldn't reopen that item — try again.");
      });
  }, []);

  const undo = useCallback(() => {
    const pendingUndo = undoable;
    if (pendingUndo === null) return;
    // Undo is the server's unwind: reopen each just-decided item.
    for (const id of pendingUndo.ids) reopen(id);
    setUndoable(null);
  }, [undoable, reopen]);

  const dismissUndo = useCallback(() => setUndoable(null), []);

  return {
    pending,
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
