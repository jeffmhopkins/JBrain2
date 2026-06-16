// Review-item model layer: the payload normalizer and the pure display helpers
// the inbox list, the detail blocks, and the bulk actions all share. The wire
// payload is `Record<string, unknown>` read defensively (docs/DESIGN.md "Review
// inbox"); `parsePayload` turns it into the typed `Parsed` view-model every
// block renders from, so layout never re-parses the raw shape.

import type { ReviewItem } from "../api/client";

export interface Proposal {
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
export interface TraceStage {
  key: string;
  name: string;
  version: string;
  summary: string;
  rows: [string, string][];
}

export function parseTrace(raw: unknown): TraceStage[] | null {
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

export interface Parsed {
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
  // low_confidence_inference cards: the canonicals nearest the proposed
  // predicate, weighted by similarity — the ranked options the correct-in-place
  // predicate picker offers when you swap the relation. Empty without an
  // embedder; the picker then falls back to manual entry only.
  predicateSuggestions: { name: string; score: number }[];
  subject: string | null;
  value: string | null;
}

/** Parse a weighted-suggestion list ([{name, score}]) read defensively off the
 * wire — shared by new_predicate (`suggestions`) and the inference predicate
 * picker (`predicate_suggestions`). */
function parseSuggestions(raw: unknown): { name: string; score: number }[] {
  return Array.isArray(raw)
    ? raw.flatMap((s: unknown): { name: string; score: number }[] => {
        if (s === null || typeof s !== "object") return [];
        const o = s as Record<string, unknown>;
        return typeof o.name === "string" && typeof o.score === "number"
          ? [{ name: o.name, score: o.score }]
          : [];
      })
    : [];
}

export function parsePayload(payload: Record<string, unknown>): Parsed {
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
    suggestions: parseSuggestions(payload.suggestions),
    predicateSuggestions: parseSuggestions(payload.predicate_suggestions),
    subject: str(payload.subject),
    value: str(payload.value),
  };
}

/** The proposals to choose among, per kind. Choices carry their own; the
 * outcome kinds synthesize accept/reject buttons from their what-happens copy.
 * There is always at least one — and "correct it" sits beside them — so reject
 * is never the only way out. */
export function proposalsFor(p: Parsed): Proposal[] {
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
export function approveActionFor(
  item: ReviewItem,
): { action: string; payload: Record<string, unknown> } | null {
  const p = parsePayload(item.payload);
  const b = p.choices.find((c) => c.action === "accept_b");
  if (b) return { action: "accept_b", payload: { choice: b.label } };
  if (p.accept !== null && !p.acceptDestructive) return { action: "accept", payload: {} };
  return null;
}

export function kindLabel(kind: string): string {
  return kind.replaceAll("_", " ");
}

export function confidenceBadge(c: number | null): { text: string; cls: string } | null {
  if (c === null) return null;
  const pct = `${Math.round(c * 100)}%`;
  if (c >= 0.75) return { text: `high · ${pct}`, cls: "conf-high" };
  if (c >= 0.5) return { text: `med · ${pct}`, cls: "conf-med" };
  return { text: `low · ${pct}`, cls: "conf-low" };
}

export function fmtWhen(item: ReviewItem): string {
  const iso = item.resolved_at ?? item.resolution?.reopened_at ?? item.created_at;
  const d = new Date(iso);
  if (d.toDateString() === new Date().toDateString())
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function decidedVerb(item: ReviewItem): string {
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

export function correctionDraft(item: ReviewItem, p: Parsed): string {
  const lead =
    item.kind === "ambiguous_mention" && p.candidateName !== null
      ? `“${p.candidateName}” here refers to `
      : item.kind === "merge_proposal"
        ? "these are "
        : "the right value is ";
  return `Correction — ${p.summary ?? kindLabel(item.kind)}.\n\n${lead}`;
}

/** Raw cosine similarity → a readable band (the card never shows the number). */
export function matchBand(score: number): { label: string; cls: string } {
  if (score >= 0.75) return { label: "strong match", cls: "lvl-strong" };
  if (score >= 0.65) return { label: "likely match", cls: "lvl-likely" };
  return { label: "weak match", cls: "lvl-weak" };
}

/** The copy-all log: a self-contained, paste-anywhere rendering of the trace —
 * the same content the console view shows, for pasting into an issue or a note. */
export function traceLog(stages: TraceStage[], factLine: string, verdictLine: string): string {
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
