import { correctionDraft } from "../payload";
import type { BlockCtx } from "./types";

/** The lane-driven footer. Pending: inferences correct in place (predicate +
 * value on the card), so they show no footer; other kinds keep "correct it", the
 * free-form correction composer. Deferred: resume. Decided: an armed reopen that
 * unwinds the decision. */
export function Footer({ ctx }: { ctx: BlockCtx }) {
  const { item, parsed, lane, queue, armed, tap, onClose, inference, composing } = ctx;

  if (lane === "pending") {
    // Inferences edit predicate + value in place — nothing left for the footer.
    if (inference.isInference) return null;
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

  if (lane === "deferred") {
    return (
      <footer className="rdetail-foot">
        <button
          type="button"
          className="rfoot-resume"
          onClick={() => {
            queue.reopen(item.id);
            onClose();
          }}
        >
          resume — back to pending
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
