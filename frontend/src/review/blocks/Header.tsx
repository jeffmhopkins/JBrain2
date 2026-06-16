import { EntityTypeIcon } from "../../entities/kinds";
import { DomainDot } from "../DomainDot";
import { reviewSubject } from "../grouping";
import { confidenceBadge, kindLabel } from "../payload";
import type { ReviewBlock } from "./types";

/** The lead of every review detail: the subject entity this item is about (its
 * type icon + name), then the meta row (kind badge · domain · confidence), the
 * hero summary, and the one-line rationale. The subject is read from the same
 * best-available signal the list groups by, so a card always names whose fact
 * it is. */
export const Header: ReviewBlock = ({ ctx }) => {
  const { item, parsed } = ctx;
  // Inference cards carry their hold weight rather than a `confidence`; surface
  // it as the confidence badge so the lead reads the same as other kinds.
  const conf = confidenceBadge(parsed.confidence ?? parsed.weight);
  const subject = reviewSubject(item);
  return (
    <>
      {subject !== null && (
        <div className="rdetail-subject">
          <EntityTypeIcon kind={subject.kind} size={34} />
          <div className="rdetail-subject-main">
            <span className="rdetail-subject-name">{subject.label}</span>
            <span className="rdetail-subject-sub">
              {subject.kind !== "Thing" && <span>{subject.kind}</span>}
              <DomainDot domain={item.domain} />
              {item.domain}
            </span>
          </div>
        </div>
      )}
      <div className="rdetail-meta">
        <span className="kind-badge">{kindLabel(item.kind)}</span>
        {subject === null && <DomainDot domain={item.domain} />}
        {conf && <span className={`conf-badge ${conf.cls}`}>{conf.text}</span>}
      </div>
      {parsed.summary !== null && <h2 className="rdetail-hero">{parsed.summary}</h2>}
      {parsed.rationale !== null && <p className="rdetail-why">{parsed.rationale}</p>}
    </>
  );
};
