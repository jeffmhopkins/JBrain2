// Review inbox — split-inbox redesign (docs/DESIGN.md "Review inbox"). Three
// lanes (pending · deferred · decided) behind a segmented filter. The list is
// browsable with a selection mode for bulk actions; tapping a row pushes a
// detail view with prev/next so you move between items without returning to
// the list. The detail shows the proposal as a before→after (collisions) or a
// what-happens panel, the cited evidence, and the proposals to choose among —
// plus two universal escape hatches, defer and "talk it over", so no item is
// ever a reject-only dead end. Every decision raises an undo snackbar (undo is
// the server's own unwind). Decided rows reopen; deferred rows resume.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MarkedText } from "../analysis/bits";
import type { ReviewItem } from "../api/client";
import type { ReviewFilter } from "../api/client";
import { DOMAIN_COLOR, DOMAIN_TITLE } from "../notes/modes";
import { type ReviewQueueController, useReviewQueue } from "../review/useReviewQueue";

const DISARM_MS = 3000;

interface Proposal {
  action: string;
  label: string;
  detail: string | null;
  destructive: boolean;
  // Extra fields a choice carries into the resolve payload (e.g. the
  // canonical_name a new_predicate map_to_existing choice must echo back).
  payload?: Record<string, unknown>;
}

interface Parsed {
  summary: string | null;
  rationale: string | null;
  snippet: string | null;
  confidence: number | null;
  accept: string | null;
  reject: string | null;
  choices: Proposal[];
  acceptDestructive: boolean;
  rejectDestructive: boolean;
  beforeLabel: string | null;
  afterLabel: string | null;
  candidateName: string | null;
}

function parsePayload(payload: Record<string, unknown>): Parsed {
  const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
  const num = (v: unknown): number | null => (typeof v === "number" ? v : null);
  const outcomes =
    payload.outcomes !== null && typeof payload.outcomes === "object"
      ? (payload.outcomes as Record<string, unknown>)
      : {};
  const choices: Proposal[] = Array.isArray(payload.choices)
    ? payload.choices.flatMap((c: unknown): Proposal[] => {
        if (c === null || typeof c !== "object") return [];
        const o = c as Record<string, unknown>;
        const action = str(o.action);
        const label = str(o.label);
        if (action === null || label === null) return [];
        const canonical = str(o.canonical_name);
        return [
          {
            action,
            label,
            detail: str(o.detail),
            destructive: o.destructive === true,
            ...(canonical !== null ? { payload: { canonical_name: canonical } } : {}),
          },
        ];
      })
    : [];
  const before = choices.find((c) => c.action === "accept_a") ?? null;
  const after = choices.find((c) => c.action === "accept_b") ?? null;
  return {
    summary: str(payload.summary),
    rationale: str(payload.rationale),
    snippet: str(payload.snippet),
    confidence: num(payload.confidence),
    accept: str(outcomes.accept),
    reject: str(outcomes.reject),
    choices,
    acceptDestructive: payload.accept_destructive === true,
    rejectDestructive: payload.reject_destructive === true,
    beforeLabel: before?.label ?? null,
    afterLabel: after?.label ?? null,
    candidateName: str(payload.name),
  };
}

/** The proposals to choose among, per kind. Choices carry their own; the
 * outcome kinds synthesize accept/reject buttons from their what-happens copy.
 * There is always at least one — and defer/discuss sit beside them — so reject
 * is never the only way out. */
function proposalsFor(p: Parsed): Proposal[] {
  if (p.choices.length > 0) return p.choices;
  const out: Proposal[] = [];
  if (p.accept !== null)
    out.push({
      action: "accept",
      label: "approve",
      detail: p.accept,
      destructive: p.acceptDestructive,
    });
  if (p.reject !== null)
    out.push({
      action: "reject",
      label: p.accept === null ? "leave unlinked" : "reject",
      detail: p.reject,
      destructive: p.rejectDestructive,
    });
  return out;
}

/** The action a bulk "approve" applies to this row, or null if it has no
 * unambiguous approve (ambiguous mentions advertise no accept). */
function approveActionFor(
  item: ReviewItem,
): { action: string; payload: Record<string, unknown> } | null {
  const p = parsePayload(item.payload);
  const b = p.choices.find((c) => c.action === "accept_b");
  if (b) return { action: "accept_b", payload: { choice: b.label } };
  if (p.accept !== null && !p.acceptDestructive) return { action: "accept", payload: {} };
  return null;
}

function kindLabel(kind: string): string {
  return kind.replaceAll("_", " ");
}

function confidenceBadge(c: number | null): { text: string; cls: string } | null {
  if (c === null) return null;
  const pct = `${Math.round(c * 100)}%`;
  if (c >= 0.75) return { text: `high · ${pct}`, cls: "conf-high" };
  if (c >= 0.5) return { text: `med · ${pct}`, cls: "conf-med" };
  return { text: `low · ${pct}`, cls: "conf-low" };
}

function fmtWhen(item: ReviewItem): string {
  const iso = item.resolved_at ?? item.resolution?.reopened_at ?? item.created_at;
  const d = new Date(iso);
  if (d.toDateString() === new Date().toDateString())
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Armed tap-again for destructive controls: first tap arms, a second within
 * 3s confirms; a timeout or any other control disarms. */
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

function DomainDot({ domain }: { domain: string }) {
  return (
    <span
      className="domain-dot"
      style={{ background: DOMAIN_COLOR[domain] ?? "var(--steel)" }}
      title={DOMAIN_TITLE[domain] ?? domain}
    />
  );
}

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
  return (
    <div className={`rrow2${dismissed ? " rrow-dismissed" : ""}`}>
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

function decidedVerb(item: ReviewItem): string {
  const a = item.resolution?.action;
  if (a === undefined) return "decided";
  if (a === "accept" || a === "accept_a" || a === "accept_b") return "approved";
  if (a === "reject") return "rejected";
  if (a === "correct") return "corrected";
  // new_predicate outcomes: a mapped predicate and a minted one read better as
  // verbs than their raw resolve actions.
  if (a === "map_to_existing" || a === "suggest_better") return "mapped";
  if (a === "accept_as_new") return "kept as new";
  return a.replaceAll("_", " ");
}

// ===== Detail =====

interface DetailProps {
  item: ReviewItem;
  lane: ReviewFilter;
  queue: ReviewQueueController;
  position: { index: number; total: number } | null;
  onClose: () => void;
  onNav: (delta: number) => void;
}

function correctionDraft(item: ReviewItem, p: Parsed): string {
  const lead =
    item.kind === "ambiguous_mention" && p.candidateName !== null
      ? `“${p.candidateName}” here refers to `
      : item.kind === "merge_proposal"
        ? "these are "
        : "the right value is ";
  return `Correction — ${p.summary ?? kindLabel(item.kind)}.\n\n${lead}`;
}

function Detail({ item, lane, queue, position, onClose, onNav }: DetailProps) {
  const p = parsePayload(item.payload);
  const [armed, tap] = useArmed();
  const [composing, setComposing] = useState(false);
  const [draft, setDraft] = useState("");
  const conf = confidenceBadge(p.confidence);
  const proposals = proposalsFor(p);
  const showDiff = p.beforeLabel !== null && p.afterLabel !== null;

  function choose(proposal: Proposal) {
    const key = `prop-${proposal.action}`;
    if (proposal.destructive && !tap(key)) return;
    queue.resolve(item.id, proposal.action, {
      choice: proposal.label,
      ...(proposal.payload ?? {}),
    });
    onClose();
  }

  function openComposer() {
    setDraft(correctionDraft(item, p));
    setComposing(true);
  }

  function fileCorrection() {
    if (draft.trim().length === 0) return;
    queue.correct(item.id, draft.trim());
    onClose();
  }

  return (
    <section className="rdetail">
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
        <div className="rdetail-meta">
          <span className="kind-badge">{kindLabel(item.kind)}</span>
          <DomainDot domain={item.domain} />
          {conf && <span className={`conf-badge ${conf.cls}`}>{conf.text}</span>}
        </div>
        {p.summary !== null && <h2 className="rdetail-hero">{p.summary}</h2>}
        {p.rationale !== null && <p className="rdetail-why">{p.rationale}</p>}

        {showDiff && (
          <div className="rdiff" aria-label="before and after">
            <div className="rdiff-row rdiff-before">
              <span className="rdiff-lbl">current</span>
              <span className="rdiff-val">
                <s>{p.beforeLabel}</s>
              </span>
            </div>
            <div className="rdiff-arrow">↓ proposed</div>
            <div className="rdiff-row rdiff-after">
              <span className="rdiff-lbl">from this note</span>
              <span className="rdiff-val">
                <ins>{p.afterLabel}</ins>
              </span>
            </div>
          </div>
        )}

        {p.candidateName !== null && (
          <p className="rdetail-cands">
            no automatic link yet — correct it, defer, or talk it over to resolve which{" "}
            {p.candidateName}.
          </p>
        )}

        {lane === "pending" ? (
          <>
            {composing && (
              <div className="rcompose">
                <h3 className="section-header">file a correction note</h3>
                <textarea
                  className="rcompose-box"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  aria-label="correction note"
                  rows={4}
                />
                <p className="rcompose-hint">
                  filed as a note in your {item.domain} domain — the pipeline applies it, so the
                  wiki stays machine-written.
                </p>
                <div className="rcompose-actions">
                  <button
                    type="button"
                    className="rcompose-cancel"
                    onClick={() => setComposing(false)}
                  >
                    cancel
                  </button>
                  <button type="button" className="rcompose-file" onClick={fileCorrection}>
                    file correction
                  </button>
                </div>
              </div>
            )}
            <h3 className="section-header">choose among proposals</h3>
            <div className="rproposals">
              {proposals.map((proposal) => {
                const key = `prop-${proposal.action}`;
                const isArmed = armed === key;
                return (
                  <button
                    key={proposal.action}
                    type="button"
                    className={`rprop${proposal.destructive ? " rprop-destructive" : ""}${
                      isArmed ? " armed" : ""
                    }`}
                    onClick={() => choose(proposal)}
                  >
                    <span className="rprop-label">
                      {isArmed ? "tap again — this is permanent" : proposal.label}
                    </span>
                    {!isArmed && proposal.detail !== null && (
                      <span className="rprop-detail">{proposal.detail}</span>
                    )}
                  </button>
                );
              })}
            </div>
          </>
        ) : (
          <DecidedRecord item={item} parsed={p} />
        )}

        {p.snippet !== null && (
          <>
            <h3 className="section-header">cited evidence</h3>
            <blockquote className="evidence">
              <MarkedText text={p.snippet} />
            </blockquote>
          </>
        )}
        {queue.actionError !== null && <p className="review-error">{queue.actionError}</p>}
      </div>

      <footer className="rdetail-foot">
        {lane === "pending" ? (
          <>
            <button
              type="button"
              className="rfoot-defer"
              onClick={() => {
                queue.resolve(item.id, "defer");
                onClose();
              }}
            >
              defer
            </button>
            <button
              type="button"
              className={`rfoot-correct${composing ? " active" : ""}`}
              onClick={() => (composing ? setComposing(false) : openComposer())}
            >
              correct it
            </button>
            <button
              type="button"
              className="rfoot-discuss"
              onClick={() => {
                queue.resolve(item.id, "discuss");
                onClose();
              }}
            >
              talk it over
            </button>
          </>
        ) : lane === "deferred" ? (
          <button
            type="button"
            className="rfoot-resume"
            onClick={() => {
              queue.reopen(item.id);
              onClose();
            }}
          >
            resume — back to pending
          </button>
        ) : item.status === "open" ? (
          <span className="rfoot-note">reopened — waiting in pending.</span>
        ) : (
          <button
            type="button"
            className={`rfoot-reopen${armed === "reopen" ? " armed" : ""}`}
            onClick={() => {
              if (tap("reopen")) {
                queue.reopen(item.id);
                onClose();
              }
            }}
          >
            {armed === "reopen" ? "tap again — decision unwound" : "reopen — unwind this decision"}
          </button>
        )}
      </footer>
    </section>
  );
}

function DecidedRecord({ item, parsed }: { item: ReviewItem; parsed: Parsed }) {
  const action = item.resolution?.action ?? null;
  const proposals = proposalsFor(parsed);
  if (item.resolution?.action === "defer" || item.resolution?.action === "discuss") {
    return <p className="rdetail-cands">parked — resume to bring it back to the pending queue.</p>;
  }
  if (action === "correct") {
    return (
      <p className="rdetail-cands">
        corrected — filed as a note; the pipeline applies your fix to the wiki.
      </p>
    );
  }
  return (
    <>
      <h3 className="section-header">what was decided</h3>
      <div className="offered">
        {proposals.map((proposal) => {
          const chosen = proposal.action === action;
          const text =
            proposal.detail !== null ? `${proposal.label} — ${proposal.detail}` : proposal.label;
          return (
            <div key={proposal.action} className={`offered-row${chosen ? " chosen" : ""}`}>
              <span className="offered-mark">{chosen ? "✓" : ""}</span>
              <span>{text}</span>
            </div>
          );
        })}
        {action === "dismiss" && (
          <div className="offered-row">dismissed — skipped without a decision</div>
        )}
      </div>
    </>
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

  // Reset selection when the lane changes out from under us.
  // biome-ignore lint/correctness/useExhaustiveDependencies: lane is the trigger.
  useEffect(() => {
    setSelecting(false);
    setSelected(new Set());
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

  return (
    <>
      {lane === "pending" && (
        <div className="rlist-tools">
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

      <div className="rlist2">
        {items.map((item) => (
          <ListRow
            key={item.id}
            item={item}
            selectable={selecting}
            selected={selected.has(item.id)}
            onToggle={() => toggle(item.id)}
            onOpen={() => onOpen(item.id)}
          />
        ))}
      </div>

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
          item={detailItem}
          lane={filter}
          queue={queue}
          position={position}
          onClose={() => setDetailId(null)}
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
