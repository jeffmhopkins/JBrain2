// The numbered References list at the foot of the article. Each entry is the
// cited note's provenance (meta + domain chip) and snippet — the [n] superscripts
// throughout the body index into it, and the citation card jumps here. The cited
// reference highlights briefly when arrived at via "jump to references".

import { MarkedText } from "../../analysis/bits";
import type { WikiReference } from "../../api/client";
import { DOMAIN_TITLE } from "../../notes/modes";

export function refDomId(n: number): string {
  return `wiki-ref-${n}`;
}

export function ReferencesList({
  references,
  highlighted,
}: {
  references: WikiReference[];
  /** The citation number to flash (set after a "jump to references" tap). */
  highlighted: number | null;
}) {
  return (
    <section>
      <h2 className="wiki-refs-head">References</h2>
      <ol className="wiki-reflist">
        {references.map((ref) => (
          <li
            id={refDomId(ref.n)}
            key={ref.n}
            className={ref.n === highlighted ? "wiki-ref-hl" : undefined}
          >
            <span className="wiki-ref-meta">{ref.meta} — </span>
            <MarkedText text={ref.snippet} />
            <span className={`wiki-dchip wiki-dchip-${ref.domain}`}>
              {(DOMAIN_TITLE[ref.domain] ?? ref.domain).toLowerCase()}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
