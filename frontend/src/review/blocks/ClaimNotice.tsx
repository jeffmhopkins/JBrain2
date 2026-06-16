import type { ReviewBlock } from "./types";

/** The ambiguous-mention notice: no automatic link yet, so "correct it" is the
 * way forward. Self-gates on the candidate name. */
export const ClaimNotice: ReviewBlock = ({ ctx }) => {
  const { candidateName } = ctx.parsed;
  if (candidateName === null) return null;
  return (
    <p className="rdetail-cands">
      no automatic link yet — correct it to resolve which {candidateName}.
    </p>
  );
};
