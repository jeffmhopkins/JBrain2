import type { ReactNode } from "react";
import { EntityTypeIcon } from "../../entities/kinds";
import type { ContradictionEntity } from "../payload";
import type { ReviewBlock } from "./types";

// Source-grounded evidence for a wiki_contradiction card (docs/reference/DESIGN.md
// "Detail composition"; concept chosen from the three-mock GUI gate,
// docs/mocks/review-wiki-contradiction-b-source.html). The linter mostly pairs
// same-source records, so the fastest disambiguator is the raw source itself:
// lead with it, tint each paired record's name where it appears, and hang each
// record's extracted facts beneath — so "two separate things vs. one conflict"
// is answerable at a glance, without leaving the inbox. Self-gates when the card
// carries no structured contradiction (older cards, every other kind).

// The two sides get fixed, stable accents (steel = A, amber = B) reused across
// the source highlight and the record cards, so the eye ties a highlighted
// source line to its record.
const SIDE_ACCENTS = ["steel", "amber"] as const;

/** Wrap each record name where it appears in the source text with its side's
 * tint, so the two flagged rows stand out among the other lines. Literal,
 * case-insensitive matching (names are extracted verbatim); longest names first
 * so a name that contains another still marks correctly. */
function highlightSource(text: string, marks: { name: string; cls: string }[]): ReactNode[] {
  const ordered = marks
    .filter((m) => m.name.trim().length > 0)
    .sort((a, b) => b.name.length - a.name.length);
  const out: ReactNode[] = [];
  let rest = text;
  let key = 0;
  while (rest.length > 0) {
    let best: { index: number; mark: { name: string; cls: string } } | null = null;
    const lower = rest.toLowerCase();
    for (const mark of ordered) {
      const at = lower.indexOf(mark.name.toLowerCase());
      if (at >= 0 && (best === null || at < best.index)) best = { index: at, mark };
    }
    if (best === null) {
      out.push(<span key={key++}>{rest}</span>);
      break;
    }
    if (best.index > 0) out.push(<span key={key++}>{rest.slice(0, best.index)}</span>);
    const matched = rest.slice(best.index, best.index + best.mark.name.length);
    out.push(
      <mark key={key++} className={`rc-hl rc-hl-${best.mark.cls}`}>
        {matched}
      </mark>,
    );
    rest = rest.slice(best.index + best.mark.name.length);
  }
  return out;
}

function RecordCard({ entity, side }: { entity: ContradictionEntity; side: number }) {
  const cls = SIDE_ACCENTS[side] ?? "steel";
  return (
    <div className={`rc-record rc-side-${cls}`}>
      <div className="rc-record-head">
        <span className="rc-dot" aria-hidden="true" />
        <EntityTypeIcon kind={entity.kind} size={26} />
        <span className="rc-record-name">{entity.name}</span>
      </div>
      {entity.facts.length > 0 ? (
        <ul className="rc-facts">
          {entity.facts.map((f, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: facts are static per card.
            <li key={i} className="rc-fact">
              <span className="rc-pred">{f.predicate}</span>
              <span className="rc-stmt">{f.statement}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="rc-facts-empty">no recorded facts</p>
      )}
    </div>
  );
}

export const ClaimContradiction: ReviewBlock = ({ ctx }) => {
  const c = ctx.parsed.contradiction;
  if (c === null) return null;
  const marks = c.entities.map((e, i) => ({
    name: e.name,
    cls: SIDE_ACCENTS[i] ?? "steel",
  }));
  return (
    <>
      {c.sources.length > 0 && (
        <>
          <h3 className="section-header">source{c.sources.length > 1 ? "s" : ""}</h3>
          {c.sources.map((s, i) => (
            <blockquote
              // biome-ignore lint/suspicious/noArrayIndexKey: sources are static per card.
              key={i}
              className="rc-source"
            >
              {highlightSource(s.text, marks)}
            </blockquote>
          ))}
        </>
      )}
      <h3 className="section-header">the two records</h3>
      <p className="rc-lead">
        both were read from the source above — compare them, then rule below.
      </p>
      <div className="rc-records">
        {c.entities.map((e, i) => (
          <RecordCard key={e.id} entity={e} side={i} />
        ))}
      </div>
    </>
  );
};
