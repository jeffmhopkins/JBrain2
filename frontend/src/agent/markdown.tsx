// A small, safe Markdown renderer for assistant responses — builds React nodes
// (never dangerouslySetInnerHTML), so there's no injection surface, and it
// degrades unclosed constructs to text so a mid-stream partial answer still
// renders. Plain-text runs are additionally scanned for temporal tokens (explicit
// dates) and rendered as quiet <time> chips with a normalized tooltip. The inline
// scanner is the seam where entity/place/citation tokens slot in later.

import { type ReactNode, useMemo } from "react";

const MONTHS =
  "Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?";
// Explicit dates only — low false-positive: ISO (2026-03-19), "March 19, 1986",
// "19 March 1986", "March 2026".
const DATE = new RegExp(
  `\\b(\\d{4}-\\d{2}-\\d{2}|(?:${MONTHS})\\.?\\s+\\d{1,2}(?:,)?\\s+\\d{4}|\\d{1,2}\\s+(?:${MONTHS})\\.?\\s+\\d{4}|(?:${MONTHS})\\.?\\s+\\d{4})\\b`,
  "gi",
);
// Inline markdown: code first (so ** inside code is literal), then bold, italic,
// links, then `[^n]` source citations. Emphasis can't hug a space (avoids
// "3 * 4 * 5").
const INLINE =
  /(`[^`]+`)|(\*\*(?! )[^*\n]+(?<! )\*\*)|(\*(?! )[^*\n]+(?<! )\*)|(\[[^\]\n]+\]\([^)\n]+\))|(\[\^\d+\])/;

const isIsoDate = (s: string): boolean => /^\d{4}-\d{2}-\d{2}$/.test(s);

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

type Cite = ((n: number) => void) | undefined;

function inline(text: string, key: string, onCite: Cite): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let n = 0;
  while (rest.length > 0) {
    const m = INLINE.exec(rest);
    if (!m) {
      out.push(...withTemporal(rest, `${key}-${n++}`));
      break;
    }
    if (m.index > 0) out.push(...withTemporal(rest.slice(0, m.index), `${key}-${n++}`));
    const tok = m[0];
    const k = `${key}-${n++}`;
    if (tok.startsWith("`")) {
      out.push(
        <code key={k} className="md-code">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith("**")) {
      out.push(<strong key={k}>{inline(tok.slice(2, -2), k, onCite)}</strong>);
    } else if (tok.startsWith("*")) {
      out.push(<em key={k}>{inline(tok.slice(1, -1), k, onCite)}</em>);
    } else if (tok.startsWith("[^")) {
      // A source citation — render the number as a tappable superscript.
      const num = Number(tok.slice(2, -1));
      out.push(
        <sup key={k} className="md-cite">
          <button type="button" onClick={() => onCite?.(num)}>
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
function paragraph(text: string, key: string, onCite: Cite): ReactNode {
  const lines = text.split("\n");
  return (
    <p key={key} className="md-p">
      {lines.flatMap((ln, i) =>
        i === 0
          ? inline(ln, `${key}-l${i}`, onCite)
          : // biome-ignore lint/suspicious/noArrayIndexKey: soft-break order is stable
            [<br key={`${key}-br${i}`} />, ...inline(ln, `${key}-l${i}`, onCite)],
      )}
    </p>
  );
}

type Block =
  | { kind: "p"; text: string }
  | { kind: "h"; level: number; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "code"; code: string }
  | { kind: "quote"; text: string };

const isSpecial = (l: string): boolean =>
  /^(#{1,6})\s+/.test(l) ||
  /^```/.test(l.trim()) ||
  /^>\s?/.test(l) ||
  /^\s*[-*+]\s+/.test(l) ||
  /^\s*\d+\.\s+/.test(l);

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
    const buf: string[] = [];
    while (i < lines.length && (lines[i] ?? "").trim() !== "" && !isSpecial(lines[i] ?? ""))
      buf.push(lines[i++] ?? "");
    blocks.push({ kind: "p", text: buf.join("\n") });
  }
  return blocks;
}

function renderBlock(b: Block, key: string, onCite: Cite): ReactNode {
  switch (b.kind) {
    case "h": {
      const Tag = `h${Math.min(b.level + 2, 6)}` as "h3" | "h4" | "h5" | "h6";
      return (
        <Tag key={key} className="md-h">
          {inline(b.text, key, onCite)}
        </Tag>
      );
    }
    case "ul":
      return (
        <ul key={key} className="md-ul">
          {b.items.map((it, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: list order is stable
            <li key={`${key}-${i}`}>{inline(it, `${key}-${i}`, onCite)}</li>
          ))}
        </ul>
      );
    case "ol":
      return (
        <ol key={key} className="md-ol">
          {b.items.map((it, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: list order is stable
            <li key={`${key}-${i}`}>{inline(it, `${key}-${i}`, onCite)}</li>
          ))}
        </ol>
      );
    case "code":
      return (
        <pre key={key} className="md-pre">
          <code>{b.code}</code>
        </pre>
      );
    case "quote":
      return (
        <blockquote key={key} className="md-quote">
          {inline(b.text, key, onCite)}
        </blockquote>
      );
    default:
      return paragraph(b.text, key, onCite);
  }
}

export function Markdown({
  text,
  onCite,
}: {
  text: string;
  /** Tap handler for a `[^n]` source citation. */
  onCite?: ((n: number) => void) | undefined;
}): ReactNode {
  const blocks = useMemo(() => parseBlocks(text), [text]);
  return <div className="md">{blocks.map((b, i) => renderBlock(b, `b${i}`, onCite))}</div>;
}
