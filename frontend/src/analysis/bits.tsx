// Small shared pieces of the analysis surfaces: kind badges, status chips,
// marked source snippets, and the citation block a fact expands into.
// Status chrome follows the softened voice — a muted "provisional" chip,
// never raw `~provisional` notation.

import type { FactOut, FactStatus } from "../api/client";
import { PinIcon } from "../components/icons";
import { splitMarks } from "../search/marks";
import { fmtConfidence, fmtTemporal } from "./format";

export function KindBadge({ kind }: { kind: string }) {
  return <span className="kind-badge">{kind}</span>;
}

/** active facts carry no chip — absence of state chrome IS the calm state. */
export function StatusChip({ status, pinned }: { status: FactStatus; pinned: boolean }) {
  if (pinned) {
    return (
      <span className="fact-chip fact-chip-pinned">
        <PinIcon size={11} /> pinned
      </span>
    );
  }
  if (status === "pending_review") {
    return <span className="fact-chip fact-chip-pending">pending review</span>;
  }
  if (status === "superseded" || status === "retracted") {
    return <span className="fact-chip fact-chip-muted">{status}</span>;
  }
  return null;
}

/** Literal <mark> spans render as amber-tint highlights, like search. */
export function MarkedText({ text }: { text: string }) {
  return (
    <>
      {splitMarks(text).map((seg, i) =>
        seg.marked ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: segments are static per text.
          <mark key={i} className="snip-mark">
            {seg.text}
          </mark>
        ) : (
          // biome-ignore lint/suspicious/noArrayIndexKey: segments are static per text.
          <span key={i}>{seg.text}</span>
        ),
      )}
    </>
  );
}

interface FactCitationProps {
  fact: FactOut;
  extractor: string | null;
}

/** The expanded citation: source words, provenance, and the no-direct-edit
 * affordance — corrections route through review/pin, the pipeline owns facts. */
export function FactCitation({ fact, extractor }: FactCitationProps) {
  const meta = [
    `reported ${fmtTemporal(fact.reported_at, "day")}`,
    ...(extractor ? [extractor] : []),
    fmtConfidence(fact.confidence),
  ].join(" · ");
  return (
    <div className="fact-citation">
      {fact.source_snippet !== null && (
        <p className="fact-source">
          <MarkedText text={fact.source_snippet} />
        </p>
      )}
      <p className="fact-statement">{fact.statement}</p>
      <p className="fact-provenance">
        {meta}
        {fact.assertion !== "asserted" && (
          <span className="fact-chip fact-chip-muted">{fact.assertion}</span>
        )}
      </p>
      <p className="fact-fix-hint">fix via review — facts aren't edited directly</p>
    </div>
  );
}
