// The per-property history sheet (docs/reference/DESIGN.md "Modal system" + "entity
// pages"): a property's revision timeline lifted off the entity page into the
// shared <Sheet>, so the page stays current-only and history is one tap away.
// Reuses the approved timeline-rail paradigm verbatim — each dot a fact citing
// its note. Machine-retracted facts (extraction errors that were never true)
// are excluded here; they are audit-only, not value history.

import { useState } from "react";
import { EdgeValue, FactCitation, StatusChip } from "../analysis/bits";
import { edgePath, factSpan } from "../analysis/format";
import type { EntityPredicate, FactOut } from "../api/client";
import { Sheet } from "../components/Sheet";

/** One dot on a predicate's timeline rail: value, span, source citation. */
function RailFact({
  fact,
  onOpenEntity,
}: {
  fact: FactOut;
  onOpenEntity: (entityId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const muted = fact.status === "superseded" || fact.status === "retracted";
  const toggle = () => setOpen((o) => !o);
  return (
    <li className={`rail-fact${muted ? " fact-superseded" : ""}`}>
      <span className="rail-dot" aria-hidden="true" />
      <div
        className="rail-body"
        // biome-ignore lint/a11y/useSemanticElements: the body hosts a nested object-entity link, which a real <button> cannot wrap.
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
          }
        }}
      >
        <span className="rail-value">
          <EdgeValue fact={fact} onOpenEntity={onOpenEntity} />
        </span>
        <span className="rail-span">
          {factSpan(fact)}
          <StatusChip status={fact.status} pinned={fact.pinned} />
        </span>
      </div>
      {open && <FactCitation fact={fact} extractor={null} />}
    </li>
  );
}

interface EntityHistorySheetProps {
  pred: EntityPredicate;
  onClose: () => void;
  /** Navigate to an object node from a rail edge; closes the sheet first. */
  onOpenEntity: (entityId: string) => void;
}

/** A property's full timeline (current + once-true superseded values), newest
 * first, in the shared bottom sheet. Retracted facts are filtered out — they
 * are machine errors, not history. */
export function EntityHistorySheet({ pred, onClose, onOpenEntity }: EntityHistorySheetProps) {
  // history is newest-first per the API contract; keep it, drop retractions.
  const timeline = pred.history.filter((f) => f.status !== "retracted");
  return (
    <Sheet title={edgePath(pred.predicate, pred.qualifier)} onClose={onClose}>
      <ul className="timeline-rail sheet-rail">
        {timeline.map((fact) => (
          <RailFact
            key={fact.id}
            fact={fact}
            onOpenEntity={(id) => {
              onClose();
              onOpenEntity(id);
            }}
          />
        ))}
      </ul>
    </Sheet>
  );
}
