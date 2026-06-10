// Review inbox (docs/DESIGN.md "Review inbox" — one-at-a-time triage):
// full-screen card per item with progress dots, the cited note text as the
// hero at editor size, a bordered "what happens" panel, candidate choices
// as big stacked buttons, and a fixed skip · reject · accept bar. Skip
// cycles client-side; destructive choices use the armed tap-again with a
// 3s auto-disarm.

import { useCallback, useEffect, useRef, useState } from "react";
import { MarkedText } from "../analysis/bits";
import type { ReviewItem } from "../api/client";
import { DOMAIN_COLOR, DOMAIN_TITLE } from "../notes/modes";
import { type ReviewQueueController, useReviewQueue } from "../review/useReviewQueue";

const DISARM_MS = 3000;

interface ParsedChoice {
  action: string;
  label: string;
  detail: string | null;
  destructive: boolean;
}

interface ParsedPayload {
  summary: string | null;
  snippet: string | null;
  accept: string | null;
  reject: string | null;
  choices: ParsedChoice[];
  acceptDestructive: boolean;
  rejectDestructive: boolean;
}

/** The contract leaves `payload` free-form; read the mock convention defensively. */
function parsePayload(payload: Record<string, unknown>): ParsedPayload {
  const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
  const outcomes =
    payload.outcomes !== null && typeof payload.outcomes === "object"
      ? (payload.outcomes as Record<string, unknown>)
      : {};
  const choices = Array.isArray(payload.choices)
    ? payload.choices.flatMap((c: unknown): ParsedChoice[] => {
        if (c === null || typeof c !== "object") return [];
        const o = c as Record<string, unknown>;
        const action = str(o.action);
        const label = str(o.label);
        if (action === null || label === null) return [];
        return [{ action, label, detail: str(o.detail), destructive: o.destructive === true }];
      })
    : [];
  return {
    summary: str(payload.summary),
    snippet: str(payload.snippet),
    accept: str(outcomes.accept),
    reject: str(outcomes.reject),
    choices,
    acceptDestructive: payload.accept_destructive === true,
    rejectDestructive: payload.reject_destructive === true,
  };
}

function kindLabel(kind: string): string {
  return kind.replaceAll("_", " ");
}

/** Armed tap-again state shared by every destructive control on the card:
 * first tap arms one control, a second within 3s confirms, anything else
 * (timeout, another control, advancing) disarms. */
function useArmed(): [string | null, (key: string) => boolean] {
  const [armed, setArmed] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );
  const tap = useCallback(
    (key: string): boolean => {
      if (timer.current !== null) clearTimeout(timer.current);
      if (armed === key) {
        setArmed(null);
        return true;
      }
      setArmed(key);
      timer.current = setTimeout(() => setArmed(null), DISARM_MS);
      return false;
    },
    [armed],
  );
  return [armed, tap];
}

interface ReviewCardProps {
  item: ReviewItem;
  queue: ReviewQueueController;
  canSkip: boolean;
}

function ReviewCard({ item, queue, canSkip }: ReviewCardProps) {
  const parsed = parsePayload(item.payload);
  const [armed, tap] = useArmed();

  function fire(
    key: string,
    destructive: boolean,
    action: string,
    payload?: Record<string, unknown>,
  ) {
    if (destructive && !tap(key)) return;
    queue.resolve(action, payload);
  }

  return (
    <div className="review-card">
      <div className="review-head">
        <span className="kind-badge">{kindLabel(item.kind)}</span>
        <span
          className="domain-dot"
          style={{ background: DOMAIN_COLOR[item.domain] ?? "var(--steel)" }}
          title={DOMAIN_TITLE[item.domain] ?? item.domain}
        />
      </div>

      {parsed.summary !== null && <p className="review-summary">{parsed.summary}</p>}

      {parsed.snippet !== null && (
        <blockquote className="review-hero">
          <MarkedText text={parsed.snippet} />
        </blockquote>
      )}

      {(parsed.accept !== null || parsed.reject !== null) && (
        <div className="review-outcomes">
          <h3 className="section-header">What happens</h3>
          {parsed.accept !== null && (
            <p className="outcome-row">
              <span className="outcome-verb outcome-accept">accept</span> — {parsed.accept}
            </p>
          )}
          {parsed.reject !== null && (
            <p className="outcome-row">
              <span className="outcome-verb outcome-reject">reject</span> — {parsed.reject}
            </p>
          )}
        </div>
      )}

      {parsed.choices.length > 0 && (
        <div className="review-choices">
          {parsed.choices.map((choice) => {
            const key = `choice-${choice.action}`;
            const isArmed = armed === key;
            return (
              <button
                key={key}
                type="button"
                className={`choice-btn${choice.destructive ? " choice-destructive" : ""}${isArmed ? " armed" : ""}`}
                onClick={() =>
                  fire(key, choice.destructive, choice.action, { choice: choice.label })
                }
              >
                <span className="choice-label">
                  {isArmed ? "tap again — this is permanent" : choice.label}
                </span>
                {!isArmed && choice.detail !== null && (
                  <span className="choice-detail">{choice.detail}</span>
                )}
              </button>
            );
          })}
        </div>
      )}

      {queue.actionError !== null && <p className="review-error">{queue.actionError}</p>}

      <footer className="review-bar">
        <button type="button" className="review-skip" disabled={!canSkip} onClick={queue.skip}>
          skip
        </button>
        <button
          type="button"
          className={`review-reject${armed === "reject" ? " armed" : ""}`}
          onClick={() => fire("reject", parsed.rejectDestructive, "reject")}
        >
          {armed === "reject" ? "tap again — permanent" : "reject"}
        </button>
        <button
          type="button"
          className={`review-accept${armed === "accept" ? " armed" : ""}`}
          onClick={() => fire("accept", parsed.acceptDestructive, "accept")}
        >
          {armed === "accept" ? "tap again — permanent" : "accept"}
        </button>
      </footer>
    </div>
  );
}

export function ReviewScreen() {
  const queue = useReviewQueue();

  if (queue.loadError) {
    return (
      <main className="screen-body">
        <p className="analysis-quiet">couldn't load the review inbox — reopen to retry.</p>
      </main>
    );
  }
  if (queue.items === null) {
    return (
      <main className="screen-body">
        <p className="analysis-quiet">loading the inbox…</p>
      </main>
    );
  }

  const current = queue.items[0];
  const total = queue.resolved + queue.items.length;

  if (current === undefined) {
    return (
      <main className="screen-body">
        <p className="analysis-quiet">inbox zero — new items arrive as notes are analyzed.</p>
      </main>
    );
  }

  return (
    <main className="screen-body review-body">
      <div className="progress-dots" aria-label={`${queue.resolved} of ${total} resolved`}>
        {Array.from({ length: total }, (_, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: dots are positional by definition.
          <span key={i} className={`progress-dot${i < queue.resolved ? " dot-done" : ""}`} />
        ))}
      </div>
      <ReviewCard key={current.id} item={current} queue={queue} canSkip={queue.items.length > 1} />
    </main>
  );
}
