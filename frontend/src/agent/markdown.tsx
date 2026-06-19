// A small, safe Markdown renderer for assistant responses — builds React nodes
// (never dangerouslySetInnerHTML), so there's no injection surface, and it
// degrades unclosed constructs to text so a mid-stream partial answer still
// renders. Plain-text runs are additionally scanned for temporal tokens (explicit
// dates) and rendered as quiet <time> chips with a normalized tooltip. The inline
// scanner is the seam where entity/place/citation tokens slot in later.
//
// Math is the one exception to the no-innerHTML rule: models emit LaTeX shorthand
// ($…$, \(…\) inline; $$…$$, \[…\] display) that reads as raw markup otherwise, so
// we typeset it with KaTeX. KaTeX parses the LaTeX itself and emits only its own
// span markup — with trust off (the default) it can't render \href/\includegraphics
// or pass arbitrary HTML through — so injecting its output is safe. A delimiter is
// only consumed when it's balanced, so an unclosed $… in a streamed partial answer
// falls through to plain text rather than swallowing the rest of the bubble.

import katex from "katex";
import "katex/dist/katex.min.css";
import { type ReactNode, useMemo } from "react";
import { DOMAIN_COLOR } from "../notes/modes";

const MONTHS =
  "Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?";
// Explicit dates only — low false-positive: ISO (2026-03-19), "March 19, 1986",
// "19 March 1986", "March 2026".
const DATE = new RegExp(
  `\\b(\\d{4}-\\d{2}-\\d{2}|(?:${MONTHS})\\.?\\s+\\d{1,2}(?:,)?\\s+\\d{4}|\\d{1,2}\\s+(?:${MONTHS})\\.?\\s+\\d{4}|(?:${MONTHS})\\.?\\s+\\d{4})\\b`,
  "gi",
);
// Inline markdown: code first (so ** inside code is literal), then math (so * and
// _ inside a formula stay literal), then bold, italic, links, then `[^n]` source
// citations. Emphasis can't hug a space (avoids "3 * 4 * 5"). Inline math comes in
// two delimiters: `$…$` (guarded so it doesn't fire on currency — not adjacent to a
// digit, and content can't open/close on a space) and `\(…\)`; `$$…$$` is display
// math that happened to land mid-line.
const INLINE =
  /(`[^`]+`)|(\$\$(?! )[^\n]+?(?<! )\$\$)|((?<!\d)\$(?![ $])[^$\n]+?(?<! )\$(?!\d))|(\\\([^\n]+?\\\))|(\*\*(?! )[^*\n]+(?<! )\*\*)|(\*(?! )[^*\n]+(?<! )\*)|(\[[^\]\n]+\]\([^)\n]+\))|(\[\^\d+\])/;

const isIsoDate = (s: string): boolean => /^\d{4}-\d{2}-\d{2}$/.test(s);

// A browsing model (gpt-oss) emits source citations in its own notation —
// 【13†L9-L13】 (fullwidth brackets, a † dagger, optional line span), sometimes in
// ASCII [13†L9-L13]. They point at the model's internal browse state, not our note
// sources, so they can't become tappable chips — strip them (with one leading space
// if present) so the prose reads clean rather than leaking the raw token.
const MODEL_CITATION = /[ \t]?(?:【[^】\n]*†[^】\n]*】|\[[^\]\n]*†[^\]\n]*\])/g;
export function stripModelCitations(text: string): string {
  return text.replace(MODEL_CITATION, "");
}

// Typeset a LaTeX fragment to KaTeX's own span markup. `throwOnError: false` makes
// KaTeX render a malformed formula as a quiet error span (no throw), so a model's
// stray macro never crashes the bubble; `strict: "ignore"` keeps unknown-command
// warnings out of the console. The try/catch is a final backstop — degrade to the
// raw source rather than blow up.
function katexHtml(latex: string, displayMode: boolean): string {
  try {
    return katex.renderToString(latex, { displayMode, throwOnError: false, strict: "ignore" });
  } catch {
    return "";
  }
}

/** A LaTeX fragment typeset with KaTeX — inline by default, display (centered,
 * own line) when `display`. The rendered markup is KaTeX's own (trust off), so the
 * innerHTML carries no injection surface. On a render failure it shows the raw
 * source so nothing silently vanishes. */
function MathPart({ latex, display }: { latex: string; display?: boolean }): ReactNode {
  const trimmed = latex.trim();
  const html = useMemo(() => katexHtml(trimmed, display ?? false), [trimmed, display]);
  if (!html) return display ? `$$${latex}$$` : `$${latex}$`;
  const cls = display ? "md-math-block" : "md-math";
  // biome-ignore lint/security/noDangerouslySetInnerHtml: KaTeX-generated markup, trust off — see file header
  return <span className={cls} dangerouslySetInnerHTML={{ __html: html }} />;
}

function parseDate(raw: string): Date | null {
  const iso = isIsoDate(raw) ? `${raw}T00:00:00Z` : raw;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function TemporalToken({ raw }: { raw: string }): ReactNode {
  const d = parseDate(raw);
  if (!d) return <time className="md-temporal">{raw}</time>;
  const title = new Intl.DateTimeFormat(undefined, {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
    ...(isIsoDate(raw) ? { timeZone: "UTC" } : {}),
  }).format(d);
  return (
    <time className="md-temporal" dateTime={d.toISOString()} title={title}>
      {raw}
    </time>
  );
}

/** Split a plain-text run into strings and temporal <time> chips. */
function withTemporal(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(DATE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    out.push(<TemporalToken key={`${key}-d${i++}`} raw={m[0]} />);
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const SAFE_URL = /^(https?:|mailto:)/i;

/** An entity a tool resolved this turn — linkified inline where any of its
 * surface forms (canonical label or an alias) appears in the answer prose. */
export interface MdEntity {
  entity_id: string;
  label: string;
  domain: string;
  aliases?: string[];
}

/** A matcher over every entity surface form, plus a lookup from a matched form
 * (lowercased) back to its entity. Null matcher when there's nothing to link. */
interface EntityIndex {
  matcher: RegExp | null;
  byForm: Map<string, MdEntity>;
}

/** An ungrounded claim the reflexion verdict flagged — the verbatim answer
 * sentence to anchor an amber ⚠ flag after, where it appears in the prose. */
export interface MdFlag {
  id: string;
  /** The verbatim answer sentence (markdown source) that failed grounding. */
  claim: string;
  /** The reason to show on tap (drawn from the matching issue). */
  reason: string;
}

/** A matcher over the ungrounded-claim sentences, plus a lookup from a matched
 * (normalized) sentence back to its flag and a set the scanner marks as it places
 * each flag — so the caller can fall back to an end-of-bubble flag for any claim
 * it couldn't anchor in the rendered prose. Null matcher when there's nothing to
 * flag. */
interface FlagIndex {
  matcher: RegExp | null;
  byNorm: Map<string, MdFlag>;
  placed: Set<string>;
}

interface Ctx {
  onCite?: ((n: number) => void) | undefined;
  onEntity?: ((entityId: string) => void) | undefined;
  onFlag?: ((flagId: string) => void) | undefined;
  openFlag?: string | null | undefined;
  index: EntityIndex;
  flags: FlagIndex;
}

const escapeRe = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Every surface form of an entity: its canonical label and any aliases. */
function surfaceForms(e: MdEntity): string[] {
  return [e.label, ...(e.aliases ?? [])].filter(Boolean);
}

/** Index the entities by surface form into a case-insensitive, word-bounded
 * matcher (longest form first, so "Celine Hopkins" wins over "Celine"). The
 * first entity to claim a form keeps it. */
function buildIndex(entities: MdEntity[]): EntityIndex {
  const byForm = new Map<string, MdEntity>();
  const forms: string[] = [];
  for (const e of entities) {
    for (const f of surfaceForms(e)) {
      const key = f.toLowerCase();
      if (!byForm.has(key)) {
        byForm.set(key, e);
        forms.push(f);
      }
    }
  }
  if (forms.length === 0) return { matcher: null, byForm };
  forms.sort((a, b) => b.length - a.length);
  const alt = forms.map(escapeRe).join("|");
  return { matcher: new RegExp(`(?<![\\p{L}\\p{N}])(?:${alt})(?![\\p{L}\\p{N}])`, "giu"), byForm };
}

/** Entities none of whose surface forms appear in the text — the caller renders
 * these as fallback chips so a surfaced entity is never left without an
 * affordance. */
export function unlinkedEntities(text: string, entities: MdEntity[]): MdEntity[] {
  const { matcher, byForm } = buildIndex(entities);
  if (!matcher) return entities;
  const linked = new Set<string>();
  for (const m of text.matchAll(matcher)) {
    const ent = byForm.get(m[0].toLowerCase());
    if (ent) linked.add(ent.entity_id);
  }
  return entities.filter((e) => !linked.has(e.entity_id));
}

/** Normalize a claim/run for matching: drop inline markdown emphasis/code markers
 * (so a claim's `**bold**` source matches the rendered run where the markers are
 * gone), collapse whitespace, lowercase. Trailing sentence punctuation is dropped
 * by the verbatim split already, so we don't strip it here. */
const normalizeClaim = (s: string): string =>
  s.replace(/[*`_]/g, "").replace(/\s+/g, " ").trim().toLowerCase();

/** Index the ungrounded claims into a matcher over their normalized text (longest
 * first, so a superset sentence wins). The first claim to claim a normalized form
 * keeps it; an empty/contentless claim is dropped (nothing to anchor). */
export function buildFlagIndex(flags: MdFlag[]): FlagIndex {
  const byNorm = new Map<string, MdFlag>();
  const forms: string[] = [];
  for (const f of flags) {
    const norm = normalizeClaim(f.claim);
    if (norm && !byNorm.has(norm)) {
      byNorm.set(norm, f);
      forms.push(norm);
    }
  }
  if (forms.length === 0) return { matcher: null, byNorm, placed: new Set() };
  forms.sort((a, b) => b.length - a.length);
  const alt = forms.map(escapeRe).join("|");
  // Case-insensitive (the run carries the source casing); whitespace in a claim
  // matches any run of whitespace, so a soft-wrapped sentence still anchors.
  const pattern = alt.replace(/ /g, "\\s+");
  return { matcher: new RegExp(`(?:${pattern})`, "gi"), byNorm, placed: new Set() };
}

/** A small amber ⚠ footnote flag placed after an ungrounded claim — tappable to
 * reveal the reason ("not in your notes"). Shared by the inline anchor and the
 * end-of-bubble fallback. */
function FlagMark({ flag, ctx, fkey }: { flag: MdFlag; ctx: Ctx; fkey: string }): ReactNode {
  const open = ctx.openFlag === flag.id;
  return (
    <span className="md-flag-wrap">
      <button
        key={fkey}
        type="button"
        className="md-flag"
        aria-expanded={open}
        aria-label="unverified claim"
        title="unverified — not grounded in your notes"
        onClick={() => ctx.onFlag?.(flag.id)}
      >
        ⚠
      </button>
      {open && (
        <span className="md-flag-note" role="note">
          {flag.reason}
        </span>
      )}
    </span>
  );
}

/** Split a plain-text run into entity links first, then temporal chips on the
 * gaps — so a name reads as prose but taps through to its entity page. When an
 * ungrounded claim sentence falls within this run, an amber ⚠ flag is appended
 * right after it (mirroring the entity inlining), and the flag is marked placed so
 * the caller doesn't double it at the bubble's end. */
function scanPlain(text: string, key: string, ctx: Ctx): ReactNode[] {
  // Flags anchor on whole sentences; match them first, render each matched
  // sentence's interior through the normal entity/temporal scan, then append the
  // flag. The gaps between flagged sentences scan normally too.
  if (ctx.flags.matcher) {
    const out: ReactNode[] = [];
    let last = 0;
    let i = 0;
    for (const m of text.matchAll(ctx.flags.matcher)) {
      const at = m.index ?? 0;
      const norm = normalizeClaim(m[0]);
      const flag = ctx.flags.byNorm.get(norm);
      // Anchor on SENTENCE boundaries: an ungrounded claim is a whole sentence
      // (claims_from splits on sentence enders), so a real occurrence starts the run
      // / follows a terminator+space / a newline, and ends the run / is followed by a
      // terminator. Without this, a claim that is a prefix of a LONGER *grounded*
      // sentence ("The roof needs replacing" inside "…replacing soon and was paid
      // for.") would inject a false warning into grounded prose. A claim that fails
      // the boundary check is left to the gap scan and the end-of-bubble fallback
      // flags it instead — degrade safely, never mis-anchor.
      const before = text.slice(0, at);
      const after = text.slice(at + m[0].length);
      const leftOk = at === 0 || /[.!?]['")\]]?\s+$/.test(before) || /\n\s*$/.test(before);
      const rightOk = after === "" || /^['")\]]?\s*[.!?]/.test(after) || /^\s*\n/.test(after);
      if (!flag || !leftOk || !rightOk) continue; // not a clean sentence match — scan as prose
      if (at > last) out.push(...scanEntities(text.slice(last, at), `${key}-g${i}`, ctx));
      // Mark the flagged TEXT (subtle amber), not just the trailing ⚠ — so the
      // reader sees *which* prose is unverified. The interior still scans for
      // entity/temporal tokens; the end-of-bubble fallback (below) carries only its
      // flag, never this highlight.
      out.push(
        <span key={`${key}-cm${i}`} className="md-claim">
          {scanEntities(m[0], `${key}-c${i}`, ctx)}
        </span>,
      );
      ctx.flags.placed.add(flag.id);
      out.push(<FlagMark key={`${key}-f${i}`} flag={flag} ctx={ctx} fkey={`${key}-fb${i}`} />);
      i++;
      last = at + m[0].length;
    }
    if (last < text.length) out.push(...scanEntities(text.slice(last), `${key}-g${i}`, ctx));
    if (out.length) return out;
  }
  return scanEntities(text, key, ctx);
}

/** The entity/temporal half of a plain run — split out so the flag scanner can
 * re-run it over a flagged sentence's interior and over the gaps. */
function scanEntities(text: string, key: string, ctx: Ctx): ReactNode[] {
  if (!ctx.index.matcher) return withTemporal(text, key);
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(ctx.index.matcher)) {
    const at = m.index ?? 0;
    if (at > last) out.push(...withTemporal(text.slice(last, at), `${key}-t${i}`));
    const label = m[0];
    const ent = ctx.index.byForm.get(label.toLowerCase());
    if (ent) {
      out.push(
        <button
          key={`${key}-e${i}`}
          type="button"
          className="md-entity"
          style={{ borderBottomColor: DOMAIN_COLOR[ent.domain] ?? "var(--text-3)" }}
          onClick={() => ctx.onEntity?.(ent.entity_id)}
        >
          {label}
        </button>,
      );
    } else {
      out.push(label);
    }
    i++;
    last = at + label.length;
  }
  if (last < text.length) out.push(...withTemporal(text.slice(last), `${key}-t${i}`));
  return out;
}

function inline(text: string, key: string, ctx: Ctx): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let n = 0;
  while (rest.length > 0) {
    const m = INLINE.exec(rest);
    if (!m) {
      out.push(...scanPlain(rest, `${key}-${n++}`, ctx));
      break;
    }
    if (m.index > 0) out.push(...scanPlain(rest.slice(0, m.index), `${key}-${n++}`, ctx));
    const tok = m[0];
    const k = `${key}-${n++}`;
    if (tok.startsWith("`")) {
      out.push(
        <code key={k} className="md-code">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith("$$")) {
      out.push(<MathPart key={k} latex={tok.slice(2, -2)} display />);
    } else if (tok.startsWith("$")) {
      out.push(<MathPart key={k} latex={tok.slice(1, -1)} />);
    } else if (tok.startsWith("\\(")) {
      out.push(<MathPart key={k} latex={tok.slice(2, -2)} />);
    } else if (tok.startsWith("**")) {
      out.push(<strong key={k}>{inline(tok.slice(2, -2), k, ctx)}</strong>);
    } else if (tok.startsWith("*")) {
      out.push(<em key={k}>{inline(tok.slice(1, -1), k, ctx)}</em>);
    } else if (tok.startsWith("[^")) {
      // A source citation — render the number as a tappable superscript.
      const num = Number(tok.slice(2, -1));
      out.push(
        <sup key={k} className="md-cite">
          <button type="button" onClick={() => ctx.onCite?.(num)}>
            {num}
          </button>
        </sup>,
      );
    } else {
      const lm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      if (lm?.[1] && lm[2] && SAFE_URL.test(lm[2])) {
        out.push(
          <a key={k} className="md-link" href={lm[2]} target="_blank" rel="noreferrer noopener">
            {lm[1]}
          </a>,
        );
      } else {
        // Not a safe link — render the label text rather than a dead/unsafe href.
        out.push(...withTemporal(lm?.[1] ?? tok, k));
      }
    }
    rest = rest.slice(m.index + tok.length);
  }
  return out;
}

/** Render one paragraph's text, turning soft newlines into line breaks. */
function paragraph(text: string, key: string, ctx: Ctx): ReactNode {
  const lines = text.split("\n");
  return (
    <p key={key} className="md-p">
      {lines.flatMap((ln, i) =>
        i === 0
          ? inline(ln, `${key}-l${i}`, ctx)
          : // biome-ignore lint/suspicious/noArrayIndexKey: soft-break order is stable
            [<br key={`${key}-br${i}`} />, ...inline(ln, `${key}-l${i}`, ctx)],
      )}
    </p>
  );
}

/** A pipe-table column's alignment from its delimiter cell (`:--`/`--:`/`:-:`);
 * null when unspecified (`---`) so the cell keeps the default left flow. */
type Align = "left" | "right" | "center" | null;

type Block =
  | { kind: "p"; text: string }
  | { kind: "h"; level: number; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "code"; code: string }
  | { kind: "quote"; text: string }
  | { kind: "table"; head: string[]; align: Align[]; rows: string[][] }
  | { kind: "math"; latex: string };

// A display-math fence opening a line: `$$` or `\[`. Returns its matching close
// delimiter, or null when the line doesn't open one.
const displayMathClose = (l: string): "$$" | "\\]" | null => {
  const t = l.trim();
  if (t.startsWith("$$")) return "$$";
  if (t.startsWith("\\[")) return "\\]";
  return null;
};

const isSpecial = (l: string): boolean =>
  /^(#{1,6})\s+/.test(l) ||
  /^```/.test(l.trim()) ||
  /^>\s?/.test(l) ||
  /^\s*[-*+]\s+/.test(l) ||
  /^\s*\d+\.\s+/.test(l) ||
  displayMathClose(l) !== null;

/** Split one pipe-table row into trimmed cells: drop a single leading/trailing
 * `|` border, then split on unescaped `|` (a `\|` is a literal pipe in a cell).
 * Models emit tables both with and without the outer borders, so both parse. */
function splitCells(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  const cells: string[] = [];
  let cur = "";
  for (let i = 0; i < s.length; i++) {
    if (s[i] === "\\" && s[i + 1] === "|") {
      cur += "|";
      i++;
    } else if (s[i] === "|") {
      cells.push(cur);
      cur = "";
    } else {
      cur += s[i];
    }
  }
  cells.push(cur);
  return cells.map((c) => c.trim());
}

/** A GFM delimiter row — every cell is dashes with optional alignment colons
 * (`---`, `:--`, `--:`, `:-:`). This is what distinguishes a table header from an
 * ordinary pipe-bearing line, so it gates table detection. */
const isDelimRow = (l: string): boolean => {
  if (!l.includes("-")) return false;
  const cells = splitCells(l);
  return cells.length > 0 && cells.every((c) => /^:?-+:?$/.test(c));
};

const parseAlign = (c: string): Align => {
  const l = c.startsWith(":");
  const r = c.endsWith(":");
  return l && r ? "center" : r ? "right" : l ? "left" : null;
};

/** A pipe table starts where a header line (any line bearing a `|`) is followed
 * by a delimiter row — checked so prose/paragraph accumulation yields to it. */
const startsTable = (lines: string[], i: number): boolean =>
  (lines[i] ?? "").includes("|") && isDelimRow(lines[i + 1] ?? "");

function parseBlocks(src: string): Block[] {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    if (/^```/.test(line.trim())) {
      i++;
      const buf: string[] = [];
      while (i < lines.length && !/^```/.test((lines[i] ?? "").trim())) buf.push(lines[i++] ?? "");
      i++; // consume closing fence (or run off the end on a partial answer)
      blocks.push({ kind: "code", code: buf.join("\n") });
      continue;
    }
    if (line.trim() === "") {
      i++;
      continue;
    }
    const mathClose = displayMathClose(line);
    if (mathClose) {
      const open = mathClose === "$$" ? "$$" : "\\[";
      const after = line.trim().slice(open.length);
      const onLine = after.indexOf(mathClose);
      if (onLine !== -1) {
        // `$$ … $$` all on one line.
        blocks.push({ kind: "math", latex: after.slice(0, onLine) });
        i++;
        continue;
      }
      // Multi-line: collect until the line bearing the close fence (or run off the
      // end on a streamed partial — render what we have so far).
      const buf = [after];
      i++;
      while (i < lines.length && !(lines[i] ?? "").includes(mathClose)) buf.push(lines[i++] ?? "");
      if (i < lines.length) {
        const closeLine = lines[i] ?? "";
        buf.push(closeLine.slice(0, closeLine.indexOf(mathClose)));
        i++;
      }
      blocks.push({ kind: "math", latex: buf.join("\n") });
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h?.[1] && h[2] !== undefined) {
      blocks.push({ kind: "h", level: h[1].length, text: h[2] });
      i++;
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i] ?? ""))
        buf.push((lines[i++] ?? "").replace(/^>\s?/, ""));
      blocks.push({ kind: "quote", text: buf.join(" ") });
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i] ?? ""))
        items.push((lines[i++] ?? "").replace(/^\s*[-*+]\s+/, ""));
      blocks.push({ kind: "ul", items });
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i] ?? ""))
        items.push((lines[i++] ?? "").replace(/^\s*\d+\.\s+/, ""));
      blocks.push({ kind: "ol", items });
      continue;
    }
    if (startsTable(lines, i)) {
      const head = splitCells(line);
      const align = splitCells(lines[i + 1] ?? "").map(parseAlign);
      i += 2;
      const rows: string[][] = [];
      // Body runs until a blank line, a different block, or a line with no pipe —
      // so the table is bounded even on a streamed partial answer.
      while (
        i < lines.length &&
        (lines[i] ?? "").trim() !== "" &&
        (lines[i] ?? "").includes("|") &&
        !isSpecial(lines[i] ?? "")
      )
        rows.push(splitCells(lines[i++] ?? ""));
      blocks.push({ kind: "table", head, align, rows });
      continue;
    }
    const buf: string[] = [];
    while (
      i < lines.length &&
      (lines[i] ?? "").trim() !== "" &&
      !isSpecial(lines[i] ?? "") &&
      !startsTable(lines, i)
    )
      buf.push(lines[i++] ?? "");
    blocks.push({ kind: "p", text: buf.join("\n") });
  }
  return blocks;
}

function renderBlock(b: Block, key: string, ctx: Ctx): ReactNode {
  switch (b.kind) {
    case "h": {
      const Tag = `h${Math.min(b.level + 2, 6)}` as "h3" | "h4" | "h5" | "h6";
      return (
        <Tag key={key} className="md-h">
          {inline(b.text, key, ctx)}
        </Tag>
      );
    }
    case "ul":
      return (
        <ul key={key} className="md-ul">
          {b.items.map((it, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: list order is stable
            <li key={`${key}-${i}`}>{inline(it, `${key}-${i}`, ctx)}</li>
          ))}
        </ul>
      );
    case "ol":
      return (
        <ol key={key} className="md-ol">
          {b.items.map((it, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: list order is stable
            <li key={`${key}-${i}`}>{inline(it, `${key}-${i}`, ctx)}</li>
          ))}
        </ol>
      );
    case "code":
      return (
        <pre key={key} className="md-pre">
          <code>{b.code}</code>
        </pre>
      );
    case "math":
      // Scroll wrapper: a wide equation stays bounded on a narrow phone (mirrors
      // the table treatment) rather than stretching the bubble.
      return (
        <div key={key} className="md-math-wrap">
          <MathPart latex={b.latex} display />
        </div>
      );
    case "quote":
      return (
        <blockquote key={key} className="md-quote">
          {inline(b.text, key, ctx)}
        </blockquote>
      );
    case "table": {
      const cols = b.head.length;
      const at = (i: number): Align => b.align[i] ?? null;
      return (
        // Scroll wrapper: a wide table stays bounded on a narrow phone rather than
        // stretching the bubble.
        <div key={key} className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {b.head.map((c, i) => (
                  <th
                    // biome-ignore lint/suspicious/noArrayIndexKey: column order is stable
                    key={`${key}-h${i}`}
                    style={at(i) ? { textAlign: at(i) ?? undefined } : undefined}
                  >
                    {inline(c, `${key}-h${i}`, ctx)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {b.rows.map((row, ri) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: row order is stable
                <tr key={`${key}-r${ri}`}>
                  {/* Render to the header's column count — pad short rows, drop
                      overflow — so a ragged model row still aligns to the grid. */}
                  {Array.from({ length: cols }, (_, ci) => (
                    <td
                      // biome-ignore lint/suspicious/noArrayIndexKey: column order is stable
                      key={`${key}-r${ri}c${ci}`}
                      style={at(ci) ? { textAlign: at(ci) ?? undefined } : undefined}
                    >
                      {inline(row[ci] ?? "", `${key}-r${ri}c${ci}`, ctx)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    default:
      return paragraph(b.text, key, ctx);
  }
}

export function Markdown({
  text,
  onCite,
  entities = [],
  onEntity,
  flags = [],
  onFlag,
  openFlag,
}: {
  text: string;
  /** Tap handler for a `[^n]` source citation. */
  onCite?: ((n: number) => void) | undefined;
  /** Entities the turn surfaced — linkified where their label appears in text. */
  entities?: MdEntity[];
  /** Tap handler for an inline entity link. */
  onEntity?: ((entityId: string) => void) | undefined;
  /** Ungrounded claims the reflexion verdict flagged — an amber ⚠ flag is placed
   * after each one where it appears in the prose. */
  flags?: MdFlag[];
  /** Tap handler for a ⚠ flag (toggles its reason note open). */
  onFlag?: ((flagId: string) => void) | undefined;
  /** The id of the flag whose reason note is currently open. */
  openFlag?: string | null | undefined;
}): ReactNode {
  const blocks = useMemo(() => parseBlocks(stripModelCitations(text)), [text]);
  const index = useMemo(() => buildIndex(entities), [entities]);
  // Fresh per render: `placed` is mutated as the blocks scan, then read below to
  // decide which flags need an end-of-bubble fallback.
  const flagIndex = useMemo(() => buildFlagIndex(flags), [flags]);
  const ctx: Ctx = { onCite, onEntity, onFlag, openFlag, index, flags: flagIndex };
  const rendered = blocks.map((b, i) => renderBlock(b, `b${i}`, ctx));
  // Graceful fallback: any flagged claim the scanner couldn't anchor in the prose
  // (a markdown split, a reworded sentence) degrades to a single end-of-bubble
  // flag that opens the same note — never crash, never mis-anchor.
  const stranded = flags.filter(
    (f) =>
      !flagIndex.placed.has(f.id) &&
      // Only the flag that owns its normalized form (a duplicate sentence shares
      // one slot and one flag), and only when it had content to anchor at all.
      flagIndex.byNorm.get(normalizeClaim(f.claim))?.id === f.id,
  );
  return (
    <div className="md">
      {rendered}
      {stranded.length > 0 && (
        <p className="md-flag-fallback">
          {stranded.map((f, i) => (
            <FlagMark key={`fb-${f.id}`} flag={f} ctx={ctx} fkey={`fbm-${i}`} />
          ))}
        </p>
      )}
    </div>
  );
}
