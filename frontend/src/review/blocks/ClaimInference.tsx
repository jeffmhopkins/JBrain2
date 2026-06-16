import { edgePath } from "../../analysis/format";
import type { ReviewBlock } from "./types";

/** The proposed-fact panel for a low-confidence inference (docs/mocks/
 * review-inference-c-correct-in-place): the `predicate → value` edge it would
 * write, rendered as the entity page does, with the value editable in place —
 * a free-text chip→input, or the members of a typed (closed-enum) predicate as
 * chips. The matching approve button lives in the action block, sharing this
 * edit state through the context. Self-gates unless this is an inference. */
export const ClaimInference: ReviewBlock = ({ ctx }) => {
  const { parsed, lane, inference } = ctx;
  if (!inference.isInference) return null;
  const { originalValue, editValue, setEditValue, editingValue, setEditingValue, valueEdited } =
    inference;

  return (
    <div className="rproposed" aria-label="proposed fact">
      <span className="rdiff-lbl">
        proposed fact
        {parsed.enumValues.length > 0 && <span className="rinf-typed">closed set</span>}
      </span>
      <span className="fact-edge">
        <span className="edge-path">{edgePath(parsed.predicate ?? "", parsed.qualifier)}</span>
        <span className="edge-arrow"> → </span>
        {lane !== "pending" || parsed.enumValues.length > 0 ? (
          <span className={`edge-value${valueEdited ? " rinf-edited" : ""}`}>
            {lane === "pending" ? editValue : originalValue}
          </span>
        ) : editingValue ? (
          <input
            className="rinf-input"
            ref={(el) => el?.focus()}
            value={editValue}
            aria-label="corrected value"
            onChange={(e) => setEditValue(e.target.value)}
            onBlur={() => setEditingValue(false)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setEditingValue(false);
            }}
          />
        ) : (
          <button
            type="button"
            className={`rinf-chip${valueEdited ? " edited" : ""}`}
            onClick={() => setEditingValue(true)}
          >
            <span className="rinf-val">{editValue}</span>
            <span className="rinf-pen" aria-hidden="true">
              ✎ edit
            </span>
          </button>
        )}
      </span>
      {lane === "pending" && parsed.enumValues.length > 0 && (
        <div className="rinf-enum">
          {parsed.enumValues.map((v) => (
            <button
              key={v}
              type="button"
              className={`rinf-enum-chip${editValue === v ? " on" : ""}`}
              aria-pressed={editValue === v}
              onClick={() => setEditValue(v)}
            >
              {v}
            </button>
          ))}
        </div>
      )}
      {lane === "pending" && (
        <p className={`rinf-status${valueEdited ? " edit" : ""}`}>
          {valueEdited ? (
            <>
              correcting <s>{originalValue}</s> → <b>{editValue.trim()}</b> — filed as a correction
              note; the pipeline applies it, so the wiki stays machine-written.
            </>
          ) : (
            (parsed.accept ?? "recorded and pinned — reprocessing won't drop it.")
          )}
        </p>
      )}
    </div>
  );
};
