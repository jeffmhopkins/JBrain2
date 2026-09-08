import { correctionDraft } from "../payload";
import type { BlockCtx } from "./types";

/** The lane-driven footer. Pending: fact-bearing cards correct in place
 * (predicate + value + modality on the card), so they show no footer; other
 * kinds keep "correct it", the free-form correction composer — unless the card
 * declares itself uncorrectable. Decided: an armed reopen that unwinds the
 * decision. */
export function Footer({ ctx }: { ctx: BlockCtx }) {
  const { item, parsed, lane, queue, armed, tap, onClose, inference, composing } = ctx;

  if (lane === "pending") {
    // Editable fact cards correct predicate + value in place — nothing left for
    // the footer (an edit files the correction note the composer otherwise would).
    if (inference.editable) return null;
    // An uncorrectable card has no composer either: "correct it" files an
    // owner_correction note in the card's OWN domain, force-superseding and pinned.
    // On a card that exists because a value was deliberately held OUT of that
    // domain (the EMR location firewall), that is the leak, offered as the exit.
    // Such a card advertises its own verb in `choices` instead.
    if (!parsed.correctable) return null;
    return (
      <footer className="rdetail-foot">
        <button
          type="button"
          className={`rfoot-correct${composing ? " active" : ""}`}
          onClick={() => {
            if (composing) {
              ctx.setComposing(false);
            } else {
              ctx.setDraft(correctionDraft(item, parsed));
              ctx.setComposing(true);
            }
          }}
        >
          correct it
        </button>
      </footer>
    );
  }

  return (
    <footer className="rdetail-foot">
      {item.status === "open" ? (
        <span className="rfoot-note">reopened — waiting in pending.</span>
      ) : (
        <button
          type="button"
          className={`rfoot-reopen${armed === "reopen" ? " armed" : ""}`}
          onClick={() => {
            if (tap("reopen")) {
              queue.reopen(item.id);
              onClose();
            }
          }}
        >
          {armed === "reopen" ? "tap again — decision unwound" : "reopen — unwind this decision"}
        </button>
      )}
    </footer>
  );
}
