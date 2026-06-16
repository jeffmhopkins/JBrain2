import { MarkedText } from "../../analysis/bits";
import type { ReviewBlock } from "./types";

/** The cited source span (provenance). Self-gates when the payload carries no
 * snippet. */
export const Evidence: ReviewBlock = ({ ctx }) => {
  if (ctx.parsed.snippet === null) return null;
  return (
    <>
      <h3 className="section-header">cited evidence</h3>
      <blockquote className="evidence">
        <MarkedText text={ctx.parsed.snippet} />
      </blockquote>
    </>
  );
};
