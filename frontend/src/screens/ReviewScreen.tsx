// Review inbox (docs/DESIGN.md "Review inbox" — split segments, settled in
// a three-way review): a segmented control with live count pills splits the
// screen into OPEN — the one-at-a-time triage flow (full-screen card with
// progress dots, the cited note text as the hero, a "what happens" panel,
// stacked choices, fixed skip · reject · accept bar) — and RESOLVED, the
// reverse-chronological decision log. Log rows expand inline into the full
// decision record (cited evidence, choices offered with the chosen one
// marked) and carry an amber tap-again reopen whose consequence text names
// the unwind; reopened rows tombstone in place (struck-through decided
// line). Destructive choices use the armed tap-again with a 3s auto-disarm.

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

/** Armed tap-again state shared by every destructive control on a pane:
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
        {item.resolution?.reopened_at !== undefined && (
          <span className="state-chip chip-reopened">reopened</span>
        )}
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

      {/* The outcomes panel advertises exactly the footer verbs the resolve
          endpoint accepts for this kind (collisions resolve through their
          choices instead): no outcome text, no button. */}
      <footer className="review-bar">
        <button type="button" className="review-skip" disabled={!canSkip} onClick={queue.skip}>
          skip
        </button>
        {parsed.reject !== null && (
          <button
            type="button"
            className={`review-reject${armed === "reject" ? " armed" : ""}`}
            onClick={() => fire("reject", parsed.rejectDestructive, "reject")}
          >
            {armed === "reject" ? "tap again — permanent" : "reject"}
          </button>
        )}
        {parsed.accept !== null && (
          <button
            type="button"
            className={`review-accept${armed === "accept" ? " armed" : ""}`}
            onClick={() => fire("accept", parsed.acceptDestructive, "accept")}
          >
            {armed === "accept" ? "tap again — permanent" : "accept"}
          </button>
        )}
      </footer>
    </div>
  );
}

interface OfferedRow {
  label: string;
  chosen: boolean;
}

/** Every choice the card advertised, with the taken one marked — collisions
 * via their choice list, verb cards via the accept/reject outcome rows. */
function offeredChoices(item: ReviewItem): OfferedRow[] {
  const parsed = parsePayload(item.payload);
  const action = item.resolution?.action ?? null;
  if (parsed.choices.length > 0) {
    return parsed.choices.map((c) => ({
      label: c.detail !== null ? `${c.label} — ${c.detail}` : c.label,
      chosen: c.action === action,
    }));
  }
  const rows: OfferedRow[] = [];
  if (parsed.accept !== null) {
    rows.push({ label: `accept — ${parsed.accept}`, chosen: action === "accept" });
  }
  if (parsed.reject !== null) {
    rows.push({ label: `reject — ${parsed.reject}`, chosen: action === "reject" });
  }
  return rows;
}

/** What was decided, in plain language: the chosen option's own copy. */
function decidedText(item: ReviewItem): string {
  const action = item.resolution?.action;
  if (action === undefined || action === "dismiss") {
    return "dismissed — skipped without a decision";
  }
  return offeredChoices(item).find((o) => o.chosen)?.label ?? action;
}

/** Consequence copy for the reopen button: name the unwind, per kind. */
function unwindText(item: ReviewItem): string {
  const base = "puts it back in the open queue";
  const action = item.resolution?.action;
  if (item.status === "dismissed" || action === "dismiss") {
    return `${base} — nothing was written, nothing to unwind`;
  }
  if (item.kind === "merge_proposal" && action === "accept") {
    return `${base} and unwinds the merge — both entities and their mentions are restored`;
  }
  if (item.kind === "merge_proposal" && action === "reject") {
    return `${base} — the distinct-from edge is permanent and stays`;
  }
  if (item.kind === "domain_promotion") {
    return `${base} — the fact returns to its prior domain, the pin is released`;
  }
  if (item.kind === "attribute_collision" || item.kind === "fact_conflict") {
    return `${base} and unwinds the decision — the pinned winner is released, the retracted value restored`;
  }
  return `${base} and unwinds the decision`;
}

/** When the decision (or its reopening) happened: time today, date before. */
function fmtWhen(item: ReviewItem): string {
  const iso = item.resolved_at ?? item.resolution?.reopened_at ?? item.created_at;
  const d = new Date(iso);
  if (d.toDateString() === new Date().toDateString()) {
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

interface ResolvedRowProps {
  item: ReviewItem;
  expanded: boolean;
  onToggle: () => void;
  armed: string | null;
  tap: (key: string) => boolean;
  onReopen: () => void;
}

function ResolvedRow({ item, expanded, onToggle, armed, tap, onReopen }: ResolvedRowProps) {
  const parsed = parsePayload(item.payload);
  // A log row whose item is open again is the reopened tombstone.
  const reopened = item.status === "open";
  const dismissed = item.status === "dismissed";
  const offered = offeredChoices(item);
  const reopenKey = `reopen-${item.id}`;
  const isArmed = armed === reopenKey;

  return (
    <div className={`rrow${dismissed ? " rrow-dismissed" : ""}${reopened ? " rrow-reopened" : ""}`}>
      <button type="button" className="rrow-btn" aria-expanded={expanded} onClick={onToggle}>
        <span className="rrow-top">
          <span className="kind-badge">{kindLabel(item.kind)}</span>
          <span
            className="domain-dot"
            style={{ background: DOMAIN_COLOR[item.domain] ?? "var(--steel)" }}
            title={DOMAIN_TITLE[item.domain] ?? item.domain}
          />
          {dismissed && <span className="state-chip chip-dismissed">dismissed</span>}
          {reopened && <span className="state-chip chip-reopened">reopened</span>}
          <span className="rrow-when">{fmtWhen(item)}</span>
        </span>
        {parsed.summary !== null && <span className="rrow-summary">{parsed.summary}</span>}
        <span className="rrow-decided">
          <span className="decided-mark">{dismissed ? "—" : "✓"}</span>
          <span className="decided-text">{decidedText(item)}</span>
        </span>
      </button>

      {expanded && (
        <div className="rrow-detail">
          {parsed.snippet !== null && (
            <>
              <h3 className="section-header">cited evidence</h3>
              <blockquote className="evidence">
                <MarkedText text={parsed.snippet} />
              </blockquote>
            </>
          )}
          {offered.length > 0 && (
            <>
              <h3 className="section-header">choices offered</h3>
              <div className="offered">
                {offered.map((o) => (
                  <div key={o.label} className={`offered-row${o.chosen ? " chosen" : ""}`}>
                    <span className="offered-mark">{o.chosen ? "✓" : ""}</span>
                    <span>{o.label}</span>
                  </div>
                ))}
              </div>
            </>
          )}
          {dismissed && (
            <p className="offered-note">skipped without a decision — nothing was written.</p>
          )}
          {reopened ? (
            <p className="reopened-note">reopened — waiting in the open queue.</p>
          ) : (
            <button
              type="button"
              className={`reopen-btn${isArmed ? " armed" : ""}`}
              onClick={() => {
                if (tap(reopenKey)) onReopen();
              }}
            >
              <span className="choice-label">
                {isArmed ? "tap again — back in the queue, decision unwound" : "reopen"}
              </span>
              {!isArmed && <span className="choice-detail">{unwindText(item)}</span>}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function ResolvedPane({ queue }: { queue: ReviewQueueController }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [armed, tap] = useArmed();
  const items = queue.resolvedItems;

  if (items === null) {
    return <p className="analysis-quiet">loading past decisions…</p>;
  }
  if (items.length === 0) {
    return <p className="analysis-quiet">no decisions yet — resolved items collect here.</p>;
  }
  return (
    <>
      <div className="rlist">
        {items.map((item) => (
          <ResolvedRow
            key={item.id}
            item={item}
            expanded={expanded === item.id}
            onToggle={() => setExpanded((cur) => (cur === item.id ? null : item.id))}
            armed={armed}
            tap={tap}
            onReopen={() => queue.reopen(item.id)}
          />
        ))}
      </div>
      {queue.actionError !== null && <p className="review-error">{queue.actionError}</p>}
    </>
  );
}

interface OpenPaneProps {
  queue: ReviewQueueController;
  onShowResolved: () => void;
}

function OpenPane({ queue, onShowResolved }: OpenPaneProps) {
  if (queue.loadError) {
    return <p className="analysis-quiet">couldn't load the review inbox — reopen to retry.</p>;
  }
  if (queue.items === null) {
    return <p className="analysis-quiet">loading the inbox…</p>;
  }

  const current = queue.items[0];
  const total = queue.resolved + queue.items.length;
  const decisions = queue.resolvedItems?.length ?? 0;

  if (current === undefined) {
    if (decisions === 0) {
      return <p className="analysis-quiet">inbox zero — new items arrive as notes are analyzed.</p>;
    }
    return (
      <p className="analysis-quiet">
        inbox zero — {decisions} past decision{decisions === 1 ? "" : "s"} in{" "}
        <button type="button" className="quiet-link" onClick={onShowResolved}>
          resolved
        </button>
        .
      </p>
    );
  }

  return (
    <>
      <div className="progress-dots" aria-label={`${queue.resolved} of ${total} resolved`}>
        {Array.from({ length: total }, (_, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: dots are positional by definition.
          <span key={i} className={`progress-dot${i < queue.resolved ? " dot-done" : ""}`} />
        ))}
      </div>
      <ReviewCard key={current.id} item={current} queue={queue} canSkip={queue.items.length > 1} />
    </>
  );
}

export function ReviewScreen() {
  const queue = useReviewQueue();
  const [seg, setSeg] = useState<"open" | "resolved">("open");

  const openCount = queue.items?.length;
  const resolvedCount = queue.resolvedItems?.length;

  return (
    <main className={`screen-body review-body${seg === "resolved" ? " review-body-log" : ""}`}>
      <div className="review-segs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={seg === "open"}
          className={`review-seg${seg === "open" ? " seg-active" : ""}`}
          onClick={() => setSeg("open")}
        >
          open
          {openCount !== undefined && <span className="seg-count">{openCount}</span>}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={seg === "resolved"}
          className={`review-seg${seg === "resolved" ? " seg-active" : ""}`}
          onClick={() => setSeg("resolved")}
        >
          resolved
          {resolvedCount !== undefined && <span className="seg-count">{resolvedCount}</span>}
        </button>
      </div>
      {seg === "open" ? (
        <OpenPane queue={queue} onShowResolved={() => setSeg("resolved")} />
      ) : (
        <ResolvedPane queue={queue} />
      )}
    </main>
  );
}
