// The citation card (docs/mocks/wiki-reader-*.html): tapping an inline [n] opens
// the source note's provenance + snippet in the shared <Sheet>, with a "jump to
// references" action that scrolls the matching numbered reference into view. A
// machine-written wiki cites every claim, so a source is always one tap away.

import { MarkedText } from "../../analysis/bits";
import type { WikiReference } from "../../api/client";
import { Sheet } from "../../components/Sheet";
import { ChevronRightIcon } from "../../components/icons";
import { wikiDomainColor } from "./domain";

export function CitationCard({
  reference,
  onClose,
  onJump,
}: {
  reference: WikiReference;
  onClose: () => void;
  /** Close the card and scroll to this reference in the list. */
  onJump: (n: number) => void;
}) {
  return (
    <Sheet title="Source" onClose={onClose}>
      <div className="wiki-cite-head">
        <span className="wiki-cite-dom" style={{ background: wikiDomainColor(reference.domain) }} />
        <span className="wiki-cite-meta">
          {reference.meta} · {reference.domain}
        </span>
      </div>
      <div className="wiki-cite-snip">
        <MarkedText text={reference.snippet} />
      </div>
      <div className="wiki-cite-actions">
        <button type="button" onClick={() => onJump(reference.n)}>
          <ChevronRightIcon size={15} />
          Jump to references
        </button>
      </div>
    </Sheet>
  );
}
