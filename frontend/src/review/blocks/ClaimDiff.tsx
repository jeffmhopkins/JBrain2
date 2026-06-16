import type { ReviewBlock } from "./types";

/** before→after value diff for collisions/conflicts (struck `current` over the
 * value from this note). Self-gates unless both labels are present. */
export const ClaimDiff: ReviewBlock = ({ ctx }) => {
  const { beforeLabel, afterLabel } = ctx.parsed;
  if (beforeLabel === null || afterLabel === null) return null;
  return (
    <div className="rdiff" aria-label="before and after">
      <div className="rdiff-row rdiff-before">
        <span className="rdiff-lbl">current</span>
        <span className="rdiff-val">
          <s>{beforeLabel}</s>
        </span>
      </div>
      <div className="rdiff-arrow">↓ proposed</div>
      <div className="rdiff-row rdiff-after">
        <span className="rdiff-lbl">from this note</span>
        <span className="rdiff-val">
          <ins>{afterLabel}</ins>
        </span>
      </div>
    </div>
  );
};
