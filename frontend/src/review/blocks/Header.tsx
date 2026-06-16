import { DomainDot } from "../DomainDot";
import { confidenceBadge, kindLabel } from "../payload";
import type { ReviewBlock } from "./types";

/** Meta row (kind badge · domain dot · confidence), the hero summary, and the
 * one-line rationale — the lead of every review detail. */
export const Header: ReviewBlock = ({ ctx }) => {
  const { item, parsed } = ctx;
  const conf = confidenceBadge(parsed.confidence);
  return (
    <>
      <div className="rdetail-meta">
        <span className="kind-badge">{kindLabel(item.kind)}</span>
        <DomainDot domain={item.domain} />
        {conf && <span className={`conf-badge ${conf.cls}`}>{conf.text}</span>}
      </div>
      {parsed.summary !== null && <h2 className="rdetail-hero">{parsed.summary}</h2>}
      {parsed.rationale !== null && <p className="rdetail-why">{parsed.rationale}</p>}
    </>
  );
};
