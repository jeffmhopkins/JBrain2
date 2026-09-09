// The "entity modified" rung of a tool step (D3 of
// docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md): every write the agent made, expandable
// from the step that made it. Not a new surface — it renders INSIDE `StepRow`'s existing
// detail, in the register the step's other rungs already use, and reuses the app's one
// diff renderer (`ClaimDiffView`) for a supersession and the shipped `predicate → value`
// edge form for everything else.
//
// Correction is conversational (D3): there is deliberately no edit affordance here. The
// owner disagrees by replying in the thread, and a control that let them fix a value in
// place would make this a second decision surface — the thing D4 exists to delete.

import type { ReactNode } from "react";
import { edgePath } from "../analysis/format";
import { DOMAIN_COLOR } from "../notes/modes";
import { ClaimDiffView } from "../review/blocks/ClaimDiff";
import { domainWord, writeVerb } from "./entityWrites";
import type { FactWrite } from "./types";

/** The domain of a write, as a dot AND its name. The word is not decoration: colour
 * alone cannot say "health", and a firewall boundary the owner cannot read is not a
 * boundary they can check. */
function DomainTag({ domain }: { domain: string }): ReactNode {
  return (
    <span className="fbw-dom">
      <span
        className="ent-dot"
        aria-hidden="true"
        style={{ background: DOMAIN_COLOR[domain] ?? "var(--text-3)" }}
      />
      {domainWord(domain)}
    </span>
  );
}

/** One write: what it says, what became of it, and where it landed. */
function WriteRow({ fact }: { fact: FactWrite }): ReactNode {
  const verb = writeVerb(fact);
  const edge =
    fact.predicate !== undefined && fact.value !== undefined ? (
      <span className="fact-edge">
        <span className="edge-path">{edgePath(fact.predicate, fact.qualifier ?? null)}</span>
        <span className="edge-arrow"> → </span>
        <span className="edge-value">{fact.value}</span>
      </span>
    ) : (
      <span className="fbw-stmt">{fact.label}</span>
    );
  return (
    <li className={`fbw-row fbw-${verb}`}>
      <div className="fbw-head">
        <span className="fbw-verb">{verb}</span>
        <DomainTag domain={fact.domain} />
        {fact.from_attachment && <span className="fbw-src">from a photo</span>}
      </div>
      {edge}
      {fact.status === "replaced" && fact.replaced !== undefined && (
        <ClaimDiffView
          before={fact.replaced}
          after={fact.value ?? fact.label}
          afterLabel="now"
          arrowLabel="↓ replaced by this note"
        />
      )}
    </li>
  );
}

/** The whole rung. Renders for a write tool even with nothing to show, because "this
 * call wrote nothing" is a fact about the note the owner is owed — the silence of an
 * absent rung would read as "not a write". */
export function EntityWrites({
  facts,
  truncated,
}: {
  facts: FactWrite[];
  truncated: boolean;
}): ReactNode {
  return (
    <>
      <div className="fb-res-lab">entities modified</div>
      {facts.length === 0 ? (
        <div className="fb-res-txt">nothing was written</div>
      ) : (
        <ul className="fbw-list">
          {facts.map((f) => (
            <WriteRow key={f.fact_id} fact={f} />
          ))}
        </ul>
      )}
      {truncated && (
        <div className="fbw-trunc">
          the call was cut short — this is only the part of it that ran
        </div>
      )}
    </>
  );
}
