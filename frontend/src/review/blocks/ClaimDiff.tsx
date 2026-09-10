import type { ReactNode } from "react";
import type { ReviewBlock } from "./types";

/** The app's ONE before→after value diff — struck `current` over the value that
 * replaces it. Presentational so the review inbox and the agent's "entity modified"
 * step render the same diff instead of growing a second one; the labels are the
 * caller's, because "from this note" is the review card's phrasing and a supersession
 * inside a conversation has its own. */
export function ClaimDiffView({
  before,
  after,
  beforeLabel = "current",
  afterLabel = "from this note",
  arrowLabel = "↓ proposed",
}: {
  before: string;
  after: string;
  beforeLabel?: string;
  afterLabel?: string;
  /** The rung between the two rows. A review card PROPOSES the after value; a write
   * the agent already made has replaced it, and must not read as still pending. */
  arrowLabel?: string;
}): ReactNode {
  return (
    <div className="rdiff" aria-label="before and after">
      <div className="rdiff-row rdiff-before">
        <span className="rdiff-lbl">{beforeLabel}</span>
        <span className="rdiff-val">
          <s>{before}</s>
        </span>
      </div>
      <div className="rdiff-arrow">{arrowLabel}</div>
      <div className="rdiff-row rdiff-after">
        <span className="rdiff-lbl">{afterLabel}</span>
        <span className="rdiff-val">
          <ins>{after}</ins>
        </span>
      </div>
    </div>
  );
}

/** before→after value diff for collisions/conflicts. Self-gates unless both labels
 * are present. */
export const ClaimDiff: ReviewBlock = ({ ctx }) => {
  const { beforeLabel, afterLabel } = ctx.parsed;
  if (beforeLabel === null || afterLabel === null) return null;
  return <ClaimDiffView before={beforeLabel} after={afterLabel} />;
};
