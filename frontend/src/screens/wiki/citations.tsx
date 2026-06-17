// Inline rendering for wiki body text. Article prose carries two inline markers:
//   - `[n]`              → a citation superscript; a tap opens the citation card
//                          (and the [n] also indexes the References list).
//   - `[label](target)`  → a wiki→wiki link. target "redlink" (or "red:slug")
//                          renders the muted "no article yet" style; anything else
//                          is a live cross-link. Non-navigating in B1 (article→
//                          article routing is a later wave) — styled per the mock.
// No markdown lib — the body is a small, contained typed-block renderer.

import { Fragment, type ReactNode } from "react";

// Either a citation `[12]` or a link `[label](target)`. Citation is tried first,
// so a bare `[5]` never parses as a (label-less) link.
const INLINE_RE = /\[(\d+)\]|\[([^\]]+)\]\(([^)]+)\)/g;

/** Split prose into text runs, `[n]` citation superscripts, and wiki links.
 * Robust to several markers in one string and to none at all. */
export function withCitations(text: string, onCite: (n: number) => void): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let key = 0;
  const re = new RegExp(INLINE_RE); // fresh lastIndex; never share the /g instance
  let match: RegExpExecArray | null = re.exec(text);
  while (match !== null) {
    if (match.index > last) {
      out.push(<Fragment key={`t${key++}`}>{text.slice(last, match.index)}</Fragment>);
    }
    if (match[1] !== undefined) {
      const n = Number(match[1]);
      out.push(
        <sup key={`c${key++}`} className="wiki-ref">
          <button
            type="button"
            className="wiki-cite"
            aria-label={`Citation ${n}`}
            onClick={() => onCite(n)}
          >
            [{n}]
          </button>
        </sup>,
      );
    } else {
      const label = match[2] ?? "";
      const target = match[3] ?? "";
      const red = target === "redlink" || target.startsWith("red:");
      out.push(
        <span
          key={`l${key++}`}
          className={red ? "wiki-redlink" : "wiki-link"}
          title={red ? "No article yet" : undefined}
        >
          {label}
        </span>,
      );
    }
    last = match.index + match[0].length;
    match = re.exec(text);
  }
  if (last < text.length) {
    out.push(<Fragment key={`t${key++}`}>{text.slice(last)}</Fragment>);
  }
  return out;
}
