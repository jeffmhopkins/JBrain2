import { correctionDraft } from "../payload";
import type { BlockCtx } from "./types";

/** The lane-driven footer of escape hatches — always present so reject is never
 * the only way out. Pending: defer · correct it · talk it over (correct it is
 * hidden for inferences, which edit in place). Deferred: resume. Decided: an
 * armed reopen that unwinds the decision. */
export function Footer({ ctx }: { ctx: BlockCtx }) {
  const { item, parsed, lane, queue, armed, tap, onClose, inference, composing } = ctx;

  if (lane === "pending") {
    return (
      <footer className="rdetail-foot">
        <button
          type="button"
          className="rfoot-defer"
          onClick={() => {
            queue.resolve(item.id, "defer");
            onClose();
          }}
        >
          defer
        </button>
        {!inference.isInference && (
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
        )}
        <button
          type="button"
          className="rfoot-discuss"
          onClick={() => {
            queue.resolve(item.id, "discuss");
            onClose();
          }}
        >
          talk it over
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
