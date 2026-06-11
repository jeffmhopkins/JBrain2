// Review-inbox controller: loads the open queue and the resolved decision
// log, skips client-side (cycle to the back of the local queue — no call),
// and resolves/reopens optimistically — the UI moves immediately, a failed
// POST rolls the move back (the useNotes optimistic-append pattern).

import { useCallback, useEffect, useRef, useState } from "react";
import { type ReviewItem, api } from "../api/client";

export interface ReviewQueueController {
  /** null until the first load resolves. */
  items: ReviewItem[] | null;
  /** The decision log (resolved + dismissed + reopened tombstones). */
  resolvedItems: ReviewItem[] | null;
  loadError: boolean;
  /** Failed resolve/reopen message; cleared on the next action. */
  actionError: string | null;
  /** Resolved-this-session count — drives the progress dots. */
  resolved: number;
  skip(): void;
  resolve(action: string, payload?: Record<string, unknown>): void;
  reopen(id: string): void;
}

export function useReviewQueue(): ReviewQueueController {
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [resolvedItems, setResolvedItems] = useState<ReviewItem[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [resolved, setResolved] = useState(0);

  // Mirrors for the callbacks: reading state there, never mutating it,
  // keeps each POST a single fire under StrictMode.
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const resolvedRef = useRef(resolvedItems);
  resolvedRef.current = resolvedItems;

  useEffect(() => {
    let stale = false;
    api
      .reviewQueue()
      .then((queue) => {
        if (!stale) setItems(queue.items);
      })
      .catch(() => {
        if (!stale) setLoadError(true);
      });
    // The log drives the resolved-count pill from first paint; a failure
    // only costs the pill — the resolved pane shows its own quiet retry.
    api
      .reviewQueue("resolved")
      .then((queue) => {
        if (!stale) setResolvedItems(queue.items);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);

  const skip = useCallback(() => {
    setActionError(null);
    setItems((prev) => {
      if (prev === null || prev.length < 2) return prev;
      const [first, ...rest] = prev;
      return first ? [...rest, first] : prev;
    });
  }, []);

  const resolve = useCallback((action: string, payload: Record<string, unknown> = {}) => {
    const current = itemsRef.current?.[0];
    if (current === undefined) return;
    setActionError(null);
    // Optimistic advance; the decision heads the log immediately. A failed
    // POST rolls the item back to the front and out of the log.
    const optimistic: ReviewItem = {
      ...current,
      status: action === "dismiss" ? "dismissed" : "resolved",
      resolution: { action, payload },
      resolved_at: new Date().toISOString(),
    };
    setItems((prev) => (prev === null ? null : prev.slice(1)));
    setResolvedItems((prev) => (prev === null ? prev : [optimistic, ...prev]));
    setResolved((n) => n + 1);
    api
      .reviewResolve(current.id, action, payload)
      .then((server) => {
        // Swap in the server record (true status + recorded effects).
        setResolvedItems((now) =>
          now === null ? now : now.map((r) => (r.id === server.id ? server : r)),
        );
      })
      .catch(() => {
        setItems((now) => (now === null ? null : [current, ...now]));
        setResolvedItems((now) => (now === null ? now : now.filter((r) => r !== optimistic)));
        setResolved((n) => n - 1);
        setActionError("couldn't save that decision — try again.");
      });
  }, []);

  const reopen = useCallback((id: string) => {
    const record = resolvedRef.current?.find((r) => r.id === id);
    if (record === undefined || record.status === "open") return;
    setActionError(null);
    // Optimistic move: tombstone the log row, queue the item at the back.
    const tombstone: ReviewItem = {
      ...record,
      status: "open",
      resolved_at: null,
      resolution: {
        ...(record.resolution ?? { action: "dismiss", payload: {} }),
        reopened_at: new Date().toISOString(),
      },
    };
    const requeued: ReviewItem = { ...tombstone };
    setResolvedItems((prev) =>
      prev === null ? prev : prev.map((r) => (r.id === id ? tombstone : r)),
    );
    setItems((prev) => (prev === null ? [requeued] : [...prev, requeued]));
    api
      .reviewReopen(id)
      .then((server) => {
        setResolvedItems((now) =>
          now === null ? now : now.map((r) => (r.id === id ? server : r)),
        );
        setItems((now) => (now === null ? now : now.map((r) => (r.id === id ? server : r))));
      })
      .catch(() => {
        setResolvedItems((now) =>
          now === null ? now : now.map((r) => (r.id === id ? record : r)),
        );
        setItems((now) => (now === null ? now : now.filter((r) => r.id !== id)));
        setActionError("couldn't reopen that decision — try again.");
      });
  }, []);

  return { items, resolvedItems, loadError, actionError, resolved, skip, resolve, reopen };
}
