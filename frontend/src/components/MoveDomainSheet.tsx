// Move-domain bottom sheet (the swipe rail's "Move" and the note view's
// longhand): the four domains as radio rows, plus the destination select for
// the destinations the omnibox already knows (health/finance). Moving to a
// destination-less domain clears the destination explicitly.

import { useState } from "react";
import { DOMAIN_COLOR, DOMAIN_TITLE, MODES } from "../notes/modes";
import type { MoveTarget } from "../notes/useNoteActions";
import { Sheet } from "./Sheet";

interface DomainOption {
  code: string;
  label: string;
  destinations: readonly string[];
}

const DOMAIN_OPTIONS: DomainOption[] = [
  { code: "general", label: DOMAIN_TITLE.general ?? "General", destinations: [] },
  {
    code: "health",
    label: DOMAIN_TITLE.health ?? "Medical",
    destinations: MODES.medical.dest?.options ?? [],
  },
  {
    code: "finance",
    label: DOMAIN_TITLE.finance ?? "Financial",
    destinations: MODES.financial.dest?.options ?? [],
  },
  { code: "location", label: DOMAIN_TITLE.location ?? "Location", destinations: [] },
];

interface MoveDomainSheetProps {
  target: MoveTarget;
  onMove: (domain: string, destination: string | null) => void;
  onClose: () => void;
}

export function MoveDomainSheet({ target, onMove, onClose }: MoveDomainSheetProps) {
  const [domain, setDomain] = useState(target.domain);
  const [destination, setDestination] = useState<string | null>(target.destination);

  const selected = DOMAIN_OPTIONS.find((o) => o.code === domain);
  const destinations = selected?.destinations ?? [];

  function pickDomain(option: DomainOption) {
    setDomain(option.code);
    // Carry the destination only when it exists under the new domain.
    setDestination(
      option.destinations.includes(target.destination ?? "") ? target.destination : null,
    );
  }

  return (
    <Sheet title="Move domain" onClose={onClose}>
      <div className="domain-rows" aria-label="Domain">
        {DOMAIN_OPTIONS.map((option) => (
          <button
            key={option.code}
            type="button"
            aria-pressed={domain === option.code}
            className={`domain-row${domain === option.code ? " domain-row-on" : ""}`}
            onClick={() => pickDomain(option)}
          >
            <span className="domain-dot" style={{ background: DOMAIN_COLOR[option.code] }} />
            {option.label}
          </button>
        ))}
      </div>
      {destinations.length > 0 && (
        <label className="sheet-field">
          Destination
          <select
            aria-label="Destination"
            value={destination ?? ""}
            onChange={(e) => setDestination(e.target.value === "" ? null : e.target.value)}
          >
            <option value="">none</option>
            {destinations.map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>
        </label>
      )}
      <button
        type="button"
        className="sheet-primary"
        onClick={() => onMove(domain, destinations.length > 0 ? destination : null)}
      >
        Move
      </button>
    </Sheet>
  );
}
