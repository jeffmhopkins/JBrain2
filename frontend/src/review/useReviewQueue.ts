// Review-inbox controller: loads the open queue, skips client-side (cycle
// to the back of the local queue — no call), and resolves optimistically —
// the card advances immediately, a failed POST rolls the item back to the
// front (the useNotes optimistic-append pattern).

import { useCallback, useEffect, useRef, useState } from "react";
import { type ReviewItem, api } from "../api/client";

export interface ReviewQueueController {
  /** null until the first load resolves. */
  items: ReviewItem[] | null;
  loadError: boolean;
  /** Failed resolve message; cleared on the next action. */
  actionError: string | null;
  /** Resolved-this-session count — drives the progress dots. */
  resolved: number;
  skip(): void;
  resolve(action: string, payload?: Record<string, unknown>): void;
}

export function useReviewQueue(): ReviewQueueController {
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [resolved, setResolved] = useState(0);

  // Mirror for resolve(): reading state in the callback, never mutating it
  // there, keeps the POST a single fire under StrictMode.
  const itemsRef = useRef(items);
  itemsRef.current = items;

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
    // Optimistic advance; a failed POST rolls the item back to the front.
    setItems((prev) => (prev === null ? null : prev.slice(1)));
    setResolved((n) => n + 1);
    api.reviewResolve(current.id, action, payload).catch(() => {
      setItems((now) => (now === null ? null : [current, ...now]));
      setResolved((n) => n - 1);
      setActionError("couldn't save that decision — try again.");
    });
  }, []);

  return { items, loadError, actionError, resolved, skip, resolve };
}
