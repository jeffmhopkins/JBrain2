// "Discuss this article" sheet (docs/mocks/wiki-reader-chosen-wikipedia.html):
// the only way to correct a machine-written article is to file a correction note
// (CLAUDE.md #7 — humans never edit the wiki directly). B1 is read-only, so this
// is the affordance + the explanation; wiring a real correction-note submit is a
// later wave. Built on the shared <Sheet>, never a bespoke modal.

import { Sheet } from "../../components/Sheet";

export function DiscussSheet({ onClose }: { onClose: () => void }) {
  return (
    <Sheet title="Discuss this article" onClose={onClose}>
      <div className="wiki-discuss-ta">Describe what's wrong and what it should say…</div>
      <p className="wiki-discuss-note">
        Filed as a note (in the relevant domain) — the pipeline applies it on the next build, so the
        wiki stays machine-written. Facts are never edited directly.
      </p>
    </Sheet>
  );
}
