import type { ReviewBlock } from "./types";

/** The ambiguous-mention notice: no automatic link yet, so the escape hatches
 * (correct / defer / talk it over) are the way forward. Self-gates on the
 * candidate name. */
export const ClaimNotice: ReviewBlock = ({ ctx }) => {
  const { candidateName } = ctx.parsed;
  if (candidateName === null) return null;
  return (
    <p className="rdetail-cands">
      no automatic link yet — correct it, defer, or talk it over to resolve which {candidateName}.
    </p>
  );
};
