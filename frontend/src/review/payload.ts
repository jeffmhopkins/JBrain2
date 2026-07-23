// Review-item model layer: the payload normalizer and the pure display helpers
// the inbox list, the detail blocks, and the bulk actions all share. The wire
// payload is `Record<string, unknown>` read defensively (docs/reference/DESIGN.md "Review
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

// One side of a wiki_contradiction card: the entity the linter paired and its
// facts (predicate → statement), so the owner sees WHAT clashes, not only that
// something did. Backfilled server-side (wiki/lint._entity_details).
export interface ContradictionEntity {
  id: string;
  name: string;
  kind: string;
  facts: { predicate: string; statement: string }[];
}

// A wiki_contradiction card's structured evidence: the two paired entities and
// the deduped source chunk(s) both were extracted from. Present only on cards
// filed after the enrichment; absent → the card's block self-gates off and the
// summary line carries alone (older cards, and every non-contradiction kind).
export interface Contradiction {
  entities: ContradictionEntity[];
  sources: { text: string }[];
}

function parseContradiction(rawEntities: unknown, rawSources: unknown): Contradiction | null {
  if (!Array.isArray(rawEntities)) return null;
  const entities = rawEntities.flatMap((e: unknown): ContradictionEntity[] => {
    if (e === null || typeof e !== "object") return [];
    const o = e as Record<string, unknown>;
    if (typeof o.id !== "string" || typeof o.name !== "string") return [];
    const facts = Array.isArray(o.facts)
      ? o.facts.flatMap((f: unknown): { predicate: string; statement: string }[] => {
          if (f === null || typeof f !== "object") return [];
          const fo = f as Record<string, unknown>;
          return typeof fo.predicate === "string" && typeof fo.statement === "string"
            ? [{ predicate: fo.predicate, statement: fo.statement }]
            : [];
        })
      : [];
    return [{ id: o.id, name: o.name, kind: typeof o.kind === "string" ? o.kind : "Thing", facts }];
  });
  // Needs both sides to be a comparison; one-sided data isn't decidable.
  if (entities.length < 2) return null;
  const sources = Array.isArray(rawSources)
    ? rawSources.flatMap((s: unknown): { text: string }[] => {
        if (s === null || typeof s !== "object") return [];
        const text = (s as Record<string, unknown>).text;
        return typeof text === "string" && text.trim().length > 0 ? [{ text }] : [];
      })
    : [];
  return { entities, sources };
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
  // An inference card's hold weight (0–1), shown as its confidence badge.
  weight: number | null;
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
  // The fact's modality (assertion: asserted/negated/hypothetical/reported/
  // question/expected), so the inference card can correct it in place. Null on
  // cards filed before it was surfaced — the card then treats it as `asserted`.
  assertion: string | null;
  statement: string | null;
  valueJson: unknown;
  // The proposed edge's OBJECT entity name when it resolved to one (an `address`
  // -> a Place). Rendered as the fact's value ahead of value_json/statement, so a
  // card shows what the analysis view shows — the linked node, not the prose
  // sentence. Null for a value-only fact; the renderer then falls to valueLabel.
  objectName: string | null;
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
  // wiki_contradiction cards: the two paired entities + their facts + the shared
  // source, so the card is decidable in place. Null for every other kind and for
  // pre-enrichment cards (the block self-gates).
  contradiction: Contradiction | null;
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
    weight: num(payload.weight),
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
    assertion: str(payload.assertion),
    statement: str(payload.statement),
    valueJson: payload.value_json,
    objectName: str(payload.object_name),
    enumValues: Array.isArray(payload.enum_values)
      ? payload.enum_values.flatMap((v: unknown): string[] => (typeof v === "string" ? [v] : []))
      : [],
    trace: parseTrace(payload.trace),
    suggestions: parseSuggestions(payload.suggestions),
    predicateSuggestions: parseSuggestions(payload.predicate_suggestions),
    subject: str(payload.subject),
    value: str(payload.value),
    contradiction: parseContradiction(payload.entities, payload.sources),
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
  // A contradiction correction names both paired records so the note the pipeline
  // re-ingests already says WHICH is right (or that they're distinct).
  if (p.contradiction !== null) {
    const [a, b] = p.contradiction.entities;
    const lead = a && b ? `“${a.name}” and “${b.name}” — ` : "these records — ";
    return `Correction — ${p.summary ?? kindLabel(item.kind)}.\n\n${lead}`;
  }
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
