// Review inbox — split-inbox redesign (docs/DESIGN.md "Review inbox"). Three
// lanes (pending · deferred · decided) behind a segmented filter. The list is
// browsable with a selection mode for bulk actions; tapping a row pushes a
// detail view with prev/next so you move between items without returning to
// the list. The detail shows the proposal as a before→after (collisions) or a
// what-happens panel; a low-confidence inference's value is editable in place
// (typed predicates pick a member) — approve unchanged records it, an edit files
// a correction note. It shows the cited evidence and the proposals to choose among —
// plus two universal escape hatches, defer and "talk it over", so no item is
// ever a reject-only dead end. Every decision raises an undo snackbar (undo is
// the server's own unwind). Decided rows reopen; deferred rows resume.

import {
  Fragment,
  type TouchEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { MarkedText } from "../analysis/bits";
import { edgePath, valueLabel } from "../analysis/format";
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

// One pipeline stage of an inference card's process trace (backend
// analysis.trace.build_trace): extraction -> integration -> arbiter, each a
// summary plus [label, value] rows. String-only and display-shaped.
interface TraceStage {
  key: string;
  name: string;
  version: string;
  summary: string;
  rows: [string, string][];
}

function parseTrace(raw: unknown): TraceStage[] | null {
  if (raw === null || typeof raw !== "object") return null;
  const stages = (raw as Record<string, unknown>).stages;
  if (!Array.isArray(stages)) return null;
  const out = stages.flatMap((s: unknown): TraceStage[] => {
    if (s === null || typeof s !== "object") return [];
    const o = s as Record<string, unknown>;
    if (typeof o.key !== "string" || typeof o.name !== "string") return [];
    const rows = Array.isArray(o.rows)
      ? o.rows.flatMap((r: unknown): [string, string][] =>
          Array.isArray(r) && r.length === 2 ? [[String(r[0]), String(r[1])]] : [],
        )
      : [];
    return [
      {
        key: o.key,
        name: o.name,
        version: typeof o.version === "string" ? o.version : "",
        summary: typeof o.summary === "string" ? o.summary : "",
        rows,
      },
    ];
  });
  return out.length > 0 ? out : null;
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
  // The structured proposal an inference card holds: the edge it would write and
  // the value it carries, so the owner sees the fact, not only the prose summary.
  predicate: string | null;
  qualifier: string | null;
  statement: string | null;
  valueJson: unknown;
  // A typed (closed-enum) predicate's members — gender → [male, female,
  // unknown]. Empty for free-text edges; drives the correct-in-place picker.
  enumValues: string[];
  // The optional verbose extraction -> integration -> arbiter trace.
  trace: TraceStage[] | null;
  // new_predicate cards: the candidate canonicals (strongest first) and the
  // triggering edge (subject + value) the card previews each mapping against.
  suggestions: { name: string; score: number }[];
  subject: string | null;
  value: string | null;
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
    predicate: str(payload.predicate),
    qualifier: str(payload.qualifier),
    statement: str(payload.statement),
    valueJson: payload.value_json,
    enumValues: Array.isArray(payload.enum_values)
      ? payload.enum_values.flatMap((v: unknown): string[] => (typeof v === "string" ? [v] : []))
      : [],
    trace: parseTrace(payload.trace),
    suggestions: Array.isArray(payload.suggestions)
      ? payload.suggestions.flatMap((s: unknown): { name: string; score: number }[] => {
          if (s === null || typeof s !== "object") return [];
          const o = s as Record<string, unknown>;
          return typeof o.name === "string" && typeof o.score === "number"
            ? [{ name: o.name, score: o.score }]
            : [];
        })
      : [],
    subject: str(payload.subject),
    value: str(payload.value),
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

/** The copy-all log: a self-contained, paste-anywhere rendering of the trace —
 * the same content the console view shows, for pasting into an issue or a note. */
function traceLog(stages: TraceStage[], factLine: string, verdictLine: string): string {
  const header = [
    "JBrain · process trace",
    `fact: ${factLine}`,
    `verdict: ${verdictLine}`,
    "──────────────────────────────",
  ].join("\n");
  const body = stages
    .map(
      (s) =>
        `${s.name.toUpperCase()}  (${s.version})\n${s.rows
          .map(([k, v]) => `  ${k} = ${v}`)
          .join("\n")}`,
    )
    .join("\n\n");
  return `${header}\n\n${body}\n`;
}

/** The optional process-trace dropdown (docs/mocks/review-process-trace-mockups
 * "Direction A"): a timeline of the three pipeline stages — tap a stage to
 * expand it — with a "show console" toggle that swaps the timeline for the dense
 * raw log in the same spot, copyable in one tap for troubleshooting. */
function ProcessTrace({
  stages,
  factLine,
  verdictLine,
}: {
  stages: TraceStage[];
  factLine: string;
  verdictLine: string;
}) {
  const [open, setOpen] = useState(false);
  const [showConsole, setShowConsole] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [copied, setCopied] = useState(false);

  function copy() {
    void navigator.clipboard?.writeText(traceLog(stages, factLine, verdictLine));
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }
  function toggleStage(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }

  return (
    <div className="rtrace">
      <button
        type="button"
        className="rtrace-toggle"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="rtrace-lead">
          how this was decided — <b>{stages.length} stages</b>
        </span>
        <span className="rtrace-chev" aria-hidden="true">
          ›
        </span>
      </button>

      {open && (
        <div className="rtrace-panel">
          <div className="rtrace-actions">
            <button type="button" className="rtrace-mode" onClick={() => setShowConsole((c) => !c)}>
              {showConsole ? "▤ show timeline" : "‹/› show console"}
            </button>
          </div>

          {showConsole ? (
            <div className="rtrace-console">
              <div className="rtrace-console-head">
                <span>raw trace · paste anywhere</span>
                <button
                  type="button"
                  className={`rtrace-copy${copied ? " done" : ""}`}
                  onClick={copy}
                >
                  {copied ? "copied ✓" : "copy"}
                </button>
              </div>
              <div className="rtrace-log">
                {stages.map((s) => (
                  <div key={s.key} className={`rtrace-block stage-${s.key}`}>
                    <div className="rtrace-cstage">
                      <span className="rtrace-dot" />
                      {s.name.toUpperCase()}
                      <span className="rtrace-cver">{s.version}</span>
                    </div>
                    {s.rows.map(([k, v]) => (
                      <div key={k} className="rtrace-cline">
                        <span className="rtrace-k">{k}</span> ={" "}
                        <span className="rtrace-v">{v}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <ol className="rtrace-timeline">
              {stages.map((s) => {
                const isOpen = expanded.has(s.key);
                return (
                  <li key={s.key} className={`rtrace-node stage-${s.key}${isOpen ? " open" : ""}`}>
                    <span className="rtrace-bullet" aria-hidden="true" />
                    <button
                      type="button"
                      className="rtrace-head"
                      aria-expanded={isOpen}
                      onClick={() => toggleStage(s.key)}
                    >
                      <span className="rtrace-stage">{s.name}</span>
                      <span className="rtrace-ver">{s.version}</span>
                    </button>
                    <p className="rtrace-summary">{s.summary}</p>
                    {isOpen && (
                      <dl className="rtrace-kv">
                        {s.rows.map(([k, v]) => (
                          <Fragment key={k}>
                            <dt>{k}</dt>
                            <dd>{v}</dd>
                          </Fragment>
                        ))}
                      </dl>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}

/** Raw cosine similarity → a readable band (the card never shows the number). */
function matchBand(score: number): { label: string; cls: string } {
  if (score >= 0.75) return { label: "strong match", cls: "lvl-strong" };
  if (score >= 0.65) return { label: "likely match", cls: "lvl-likely" };
  return { label: "weak match", cls: "lvl-weak" };
}

/** new_predicate card — Direction A (docs/mocks/new-predicate-mockups.html): the
 * triggering fact as context, then the candidate canonicals as a ranked list —
 * each with a match-strength bar and a preview of the edge mapping would write —
 * with keep-as-new, rename, and dismiss grouped below as the fallbacks. */
function NewPredicateCard({
  parsed,
  onMap,
  onKeep,
  onRename,
  onDismiss,
}: {
  parsed: Parsed;
  onMap: (name: string) => void;
  onKeep: () => void;
  onRename: (name: string) => void;
  onDismiss: () => void;
}) {
  const [name, setName] = useState("");
  const subject = parsed.subject ?? "this";
  const value = parsed.value ?? parsed.statement ?? "?";
  const pred = parsed.predicate ?? "";
  return (
    <div className="rnp">
      <div className="rnp-context">
        <span className="rnp-lbl">unrecognized relation · committed under its raw name</span>
        <span className="rnp-edge">
          <span className="rnp-subj">{subject}</span>
          <span className="rnp-bracket"> —[</span>
          <span className="rnp-unknown">{pred}</span>
          <span className="rnp-bracket">]→ </span>
          <span className="rnp-val">{value}</span>
        </span>
        {parsed.statement !== null && <span className="rnp-quote">“{parsed.statement}”</span>}
      </div>

      {parsed.suggestions.length > 0 && (
        <>
          <h3 className="section-header">map it to a known relation</h3>
          <div className="rnp-opts">
            {parsed.suggestions.map((s, i) => {
              const band = matchBand(s.score);
              return (
                <button
                  key={s.name}
                  type="button"
                  className={`rnp-opt${i === 0 ? " best" : ""}`}
                  onClick={() => onMap(s.name)}
                >
                  <span className="rnp-opt-main">
                    <span className="rnp-opt-top">
                      <span className="rnp-opt-name">{s.name}</span>
                      {i === 0 && <span className="rnp-tag-best">best match</span>}
                      <span className={`rnp-match ${band.cls}`}>
                        <span className="rnp-bar">
                          <i style={{ width: `${Math.round(s.score * 100)}%` }} />
                        </span>
                        {band.label}
                      </span>
                    </span>
                    <span className="rnp-opt-prev">
                      → <b>{`${subject}.${s.name} → ${value}`}</b>
                    </span>
                  </span>
                  <span className="rnp-opt-go" aria-hidden="true">
                    ›
                  </span>
                </button>
              );
            })}
          </div>
        </>
      )}

      <div className="rnp-divider">or</div>

      <button type="button" className="rnp-minor" onClick={onKeep}>
        <span className="rnp-minor-l">
          Keep <code className="rnp-code">{pred}</code> as a new relation
        </span>
        <span className="rnp-minor-d">registers it as its own canonical predicate, used as-is</span>
      </button>

      <div className="rnp-rename">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="rename the relation"
          placeholder="…or rename it, e.g. spouse"
        />
        <button
          type="button"
          disabled={name.trim().length === 0}
          onClick={() => onRename(name.trim())}
        >
          use
        </button>
      </div>

      <button type="button" className="rnp-minor danger" onClick={onDismiss}>
        <span className="rnp-minor-l">Dismiss</span>
        <span className="rnp-minor-d">leave the fact under its raw name, clear this card</span>
      </button>
    </div>
  );
}

function Detail({ item, lane, queue, position, onClose, onNav }: DetailProps) {
  const p = parsePayload(item.payload);
  const [armed, tap] = useArmed();
  const [composing, setComposing] = useState(false);
  const [draft, setDraft] = useState("");
  const conf = confidenceBadge(p.confidence);
  const proposals = proposalsFor(p);
  const showDiff = p.beforeLabel !== null && p.afterLabel !== null;

  // Direction C — correct in place: a low-confidence inference's value is
  // editable on the card. Approve unchanged records the inference; an edit files
  // a correction note (the #7 channel) so the wiki stays machine-written. A typed
  // predicate (p.enumValues) offers its members as chips instead of free text.
  const isInference = item.kind === "low_confidence_inference" && p.predicate !== null;
  const originalValue = isInference ? valueLabel(p.valueJson, p.statement ?? "") : "";
  const [editValue, setEditValue] = useState(originalValue);
  const [editingValue, setEditingValue] = useState(false);
  const valueEdited =
    isInference && editValue.trim().length > 0 && editValue.trim() !== originalValue;

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

  function approveInference() {
    if (valueEdited) {
      const path = edgePath(p.predicate ?? "", p.qualifier);
      const body = `Correction — ${p.statement ?? p.summary ?? kindLabel(item.kind)}\n\nThe value for ${path} should be ${editValue.trim()}, not ${originalValue}.`;
      queue.correct(item.id, body);
    } else {
      queue.resolve(item.id, "accept", { choice: "approve" });
    }
    onClose();
  }
  function rejectInference() {
    if (p.rejectDestructive && !tap("inf-reject")) return;
    queue.resolve(item.id, "reject", { choice: "reject" });
    onClose();
  }

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
        <div className="rdetail-meta">
          <span className="kind-badge">{kindLabel(item.kind)}</span>
          <DomainDot domain={item.domain} />
          {conf && <span className={`conf-badge ${conf.cls}`}>{conf.text}</span>}
        </div>
        {p.summary !== null && <h2 className="rdetail-hero">{p.summary}</h2>}
        {p.rationale !== null && <p className="rdetail-why">{p.rationale}</p>}

        {isInference && (
          <div className="rproposed" aria-label="proposed fact">
            <span className="rdiff-lbl">
              proposed fact
              {p.enumValues.length > 0 && <span className="rinf-typed">closed set</span>}
            </span>
            <span className="fact-edge">
              <span className="edge-path">{edgePath(p.predicate ?? "", p.qualifier)}</span>
              <span className="edge-arrow"> → </span>
              {lane !== "pending" || p.enumValues.length > 0 ? (
                <span className={`edge-value${valueEdited ? " rinf-edited" : ""}`}>
                  {lane === "pending" ? editValue : originalValue}
                </span>
              ) : editingValue ? (
                <input
                  className="rinf-input"
                  ref={(el) => el?.focus()}
                  value={editValue}
                  aria-label="corrected value"
                  onChange={(e) => setEditValue(e.target.value)}
                  onBlur={() => setEditingValue(false)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") setEditingValue(false);
                  }}
                />
              ) : (
                <button
                  type="button"
                  className={`rinf-chip${valueEdited ? " edited" : ""}`}
                  onClick={() => setEditingValue(true)}
                >
                  <span className="rinf-val">{editValue}</span>
                  <span className="rinf-pen" aria-hidden="true">
                    ✎ edit
                  </span>
                </button>
              )}
            </span>
            {lane === "pending" && p.enumValues.length > 0 && (
              <div className="rinf-enum">
                {p.enumValues.map((v) => (
                  <button
                    key={v}
                    type="button"
                    className={`rinf-enum-chip${editValue === v ? " on" : ""}`}
                    aria-pressed={editValue === v}
                    onClick={() => setEditValue(v)}
                  >
                    {v}
                  </button>
                ))}
              </div>
            )}
            {lane === "pending" && (
              <p className={`rinf-status${valueEdited ? " edit" : ""}`}>
                {valueEdited ? (
                  <>
                    correcting <s>{originalValue}</s> → <b>{editValue.trim()}</b> — filed as a
                    correction note; the pipeline applies it, so the wiki stays machine-written.
                  </>
                ) : (
                  (p.accept ?? "recorded and pinned — reprocessing won't drop it.")
                )}
              </p>
            )}
          </div>
        )}

        {p.trace !== null && (
          <ProcessTrace
            stages={p.trace}
            factLine={`${edgePath(p.predicate ?? "", p.qualifier)} → ${valueLabel(
              p.valueJson,
              p.statement ?? "",
            )}`}
            verdictLine={conf?.text ?? "held for review"}
          />
        )}

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
            {item.kind === "new_predicate" ? (
              <NewPredicateCard
                parsed={p}
                onMap={(canonical) => {
                  queue.resolve(item.id, "map_to_existing", {
                    choice: canonical,
                    canonical_name: canonical,
                  });
                  onClose();
                }}
                onKeep={() => {
                  queue.resolve(item.id, "accept_as_new");
                  onClose();
                }}
                onRename={(canonical) => {
                  queue.resolve(item.id, "suggest_better", { canonical_name: canonical });
                  onClose();
                }}
                onDismiss={() => {
                  queue.resolve(item.id, "reject");
                  onClose();
                }}
              />
            ) : isInference ? (
              <div className="rinf-actions">
                <button
                  type="button"
                  className={`rinf-approve${valueEdited ? " correction" : ""}`}
                  onClick={approveInference}
                >
                  {valueEdited ? "approve correction" : "approve"}
                </button>
                <button
                  type="button"
                  className={`rinf-reject${armed === "inf-reject" ? " armed" : ""}`}
                  onClick={rejectInference}
                >
                  {armed === "inf-reject" ? "tap again — discard" : "reject — discard"}
                </button>
              </div>
            ) : (
              <>
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
            )}
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
            {!isInference && (
              <button
                type="button"
                className={`rfoot-correct${composing ? " active" : ""}`}
                onClick={() => (composing ? setComposing(false) : openComposer())}
              >
                correct it
              </button>
            )}
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

/** The decided record for a new_predicate card — Direction C (docs/mocks/
 * decided-view-mockups.html): a before→after diff of the change the decision
 * made. Derived entirely from the resolution + payload — no re-ticking of the
 * offered rows (which all share one map_to_existing action and so all ticked). */
function DecidedNewPredicate({ item, parsed }: { item: ReviewItem; parsed: Parsed }) {
  const action = item.resolution?.action ?? null;
  const named = item.resolution?.payload.canonical_name;
  const canonical = typeof named === "string" ? named : null;
  const before = parsed.predicate ?? "";
  const subject = parsed.subject ?? "this";
  const value = parsed.value ?? parsed.statement ?? "?";

  let after = before;
  let verb = "Decided";
  let tone = "tone-muted";
  let nowSub = "";
  if (action === "map_to_existing" && canonical !== null) {
    after = canonical;
    verb = "Mapped to";
    tone = "tone-ok";
    nowSub = "an existing relation it now uses";
  } else if (action === "suggest_better" && canonical !== null) {
    after = canonical;
    verb = "Renamed to";
    tone = "tone-ok";
    nowSub = "your canonical — the fact now uses it";
  } else if (action === "accept_as_new") {
    verb = "Kept as new";
    tone = "tone-steel";
    nowSub = "registered as its own canonical relation";
  } else {
    verb = "Dismissed";
    nowSub = "left under its raw name — unchanged";
  }

  return (
    <div className={`rdc ${tone}`}>
      <div className="rdc-row rdc-was">
        <span className="rdc-lbl">was</span>
        <span className="rdc-pred was">{before}</span>
        <span className="rdc-sub">unrecognized — coined from the note</span>
      </div>
      <div className="rdc-mid">
        <span className="rdc-ln" />
        {after !== before ? `${verb} ${after}` : verb}
        <span className="rdc-ln" />
      </div>
      <div className="rdc-row rdc-now">
        <span className="rdc-lbl">now</span>
        <span className="rdc-pred now">{after}</span>
        <span className="rdc-sub">{nowSub}</span>
        <span className="rdc-edge">
          → <b>{`${subject}.${after} → ${value}`}</b>
        </span>
      </div>
    </div>
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
  // new_predicate states its outcome as a before→after diff (map/keep/rename/
  // dismiss), so it never re-ticks the offered rows.
  if (item.kind === "new_predicate") {
    return <DecidedNewPredicate item={item} parsed={parsed} />;
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
          key={detailItem.id}
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
