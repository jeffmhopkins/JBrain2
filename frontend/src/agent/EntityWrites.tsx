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
import { domainWord, writeVerb, writeWord } from "./entityWrites";
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

/** Why the write path held a fact, or what it noticed going ahead — in the owner's
 * words, and only for the reasons that are HIS to act on. `attribute_collision` is the
 * one that matters: the value on file disagreed, and this one is live because it is
 * newer, not because anything decided between them. That sentence has only ever existed
 * inside the free-text result the MODEL reads, so a contradicted supersession has looked
 * on screen exactly like a clean one. Undefined for a reason with no owner-facing
 * meaning — a code he cannot act on is a code he has to ask about. */
function holdSentence(reason: string | undefined): string | undefined {
  if (reason === undefined || reason === "") return undefined;
  // The write path's own words (`analysis/pipeline`, `analysis/supersession.decide`).
  const said: Record<string, string> = {
    attribute_collision:
      "your notes disagreed — this one is live because it is newer, not because anyone checked which is right",
    fact_conflict: "it clashes with a value already on file, so it is recorded but not live",
    low_confidence: "recorded but not live — the reading behind it was not confident",
    "still held": "still not live — it was held before this pass, and restating it changed nothing",
    review: "recorded but not live until it is reviewed",
  };
  return said[reason];
}

/** One write: what it says, what became of it, and where it landed. A supersession
 * shows the predicate and hands the two VALUES to the diff, rather than printing the
 * new value twice — the diff is where before and after belong. */
function WriteRow({ fact }: { fact: FactWrite }): ReactNode {
  const verb = writeVerb(fact);
  const path =
    fact.predicate === undefined ? null : edgePath(fact.predicate, fact.qualifier ?? null);
  // `verb`, not `fact.status`: the state has one resolver, and asking the raw field
  // here would put the diff on a different reading of the same write than the chip.
  const superseded = verb === "replaced" && fact.replaced !== undefined;
  const held = holdSentence(fact.hold_reason);
  return (
    <li className={`fbw-row fbw-${verb}`}>
      <div className="fbw-head">
        <span className="fbw-verb">{writeWord(verb)}</span>
        <DomainTag domain={fact.domain} />
        {fact.from_attachment && <span className="fbw-src">from a photo</span>}
      </div>
      {superseded ? (
        <>
          {path !== null && <span className="edge-path">{path}</span>}
          <ClaimDiffView
            before={fact.replaced as string}
            after={fact.value ?? fact.label}
            afterLabel="now"
            arrowLabel="↓ this note updated it — the old value is kept as history"
          />
        </>
      ) : (
        <>
          {/* The STATEMENT leads. ⟲ `predicate → value` led here, and it is the graph's
              own form: it is what the entity page and the review inbox show, where the
              owner has already chosen an entity and a predicate is the column he is
              reading down. In a conversation he has chosen nothing — the sentence is the
              only thing that says which of his facts this row is — and "size → 60in"
              over a note about a television reads as a database dump of his own words.
              The edge follows it, in its shipped form, for the reader who wants the
              address. */}
          <span className="fbw-stmt">{fact.label}</span>
          {path !== null && fact.value !== undefined && (
            <span className="fact-edge fbw-edge">
              <span className="edge-path">{path}</span>
              <span className="edge-arrow"> → </span>
              <span className="edge-value">{fact.value}</span>
            </span>
          )}
        </>
      )}
      {held !== undefined && <span className="fbw-why">{held}</span>}
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
