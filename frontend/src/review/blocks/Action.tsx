import { edgePath } from "../../analysis/format";
import type { ReviewItem } from "../../api/client";
import { type Parsed, type Proposal, kindLabel, proposalsFor } from "../payload";
import { NewPredicateCard } from "./NewPredicateCard";
import type { InferenceEdit, ReviewBlock } from "./types";

/** The correction note an edited inference files (the #7 channel — humans never
 * touch the wiki, they describe the fix and the pipeline applies it). Spells out
 * whichever side(s) changed: the relation, the value, or both. */
export function inferenceCorrection(item: ReviewItem, parsed: Parsed, inf: InferenceEdit): string {
  const fromPath = edgePath(inf.originalPredicate, parsed.qualifier);
  const toPath = edgePath(inf.editPredicate.trim(), parsed.qualifier);
  const value = inf.editValue.trim();
  const lead = `Correction — ${parsed.statement ?? parsed.summary ?? kindLabel(item.kind)}`;
  let fix: string;
  if (inf.predicateEdited && inf.valueEdited) {
    fix = `This should be ${toPath} → ${value}, not ${fromPath} → ${inf.originalValue}.`;
  } else if (inf.predicateEdited) {
    fix = `The relation should be ${toPath}, not ${fromPath} (value ${inf.originalValue}).`;
  } else {
    fix = `The value for ${fromPath} should be ${value}, not ${inf.originalValue}.`;
  }
  return `${lead}\n\n${fix}`;
}

/** The polymorphic decision block: the correction-note composer, then the
 * controls a pending item offers (new_predicate map/keep/rename/dismiss, an
 * inference's approve/reject, or the generic choose-among-proposals), or — in
 * the decided lane — the record of what was decided. */
export const Action: ReviewBlock = ({ ctx }) => {
  const { item, parsed, lane, queue, armed, tap, onClose, onAdvance, inference, composing, draft } =
    ctx;
  const proposals = proposalsFor(parsed);

  function choose(proposal: Proposal) {
    const key = `prop-${proposal.action}`;
    if (proposal.destructive && !tap(key)) return;
    queue.resolve(item.id, proposal.action, {
      choice: proposal.label,
      ...(proposal.payload ?? {}),
    });
    onClose();
  }

  function fileCorrection() {
    if (draft.trim().length === 0) return;
    queue.correct(item.id, draft.trim());
    onClose();
  }

  function approveInference() {
    if (inference.edited) {
      queue.correct(item.id, inferenceCorrection(item, parsed, inference));
    } else {
      queue.resolve(item.id, "accept", { choice: "approve" });
    }
    onAdvance();
  }
  function rejectInference() {
    if (parsed.rejectDestructive && !tap("inf-reject")) return;
    queue.resolve(item.id, "reject", { choice: "reject" });
    onAdvance();
  }

  if (lane !== "pending") return <DecidedRecord item={item} parsed={parsed} />;

  return (
    <>
      {composing && (
        <div className="rcompose">
          <h3 className="section-header">file a correction note</h3>
          <textarea
            className="rcompose-box"
            value={draft}
            onChange={(e) => ctx.setDraft(e.target.value)}
            aria-label="correction note"
            rows={4}
          />
          <p className="rcompose-hint">
            filed as a note in your {item.domain} domain — the pipeline applies it, so the wiki
            stays machine-written.
          </p>
          <div className="rcompose-actions">
            <button
              type="button"
              className="rcompose-cancel"
              onClick={() => ctx.setComposing(false)}
            >
              cancel
            </button>
            <button type="button" className="rcompose-file" onClick={fileCorrection}>
              file correction
            </button>
          </div>
        </div>
      )}
      {item.kind === "new_predicate" ? (
        <NewPredicateCard
          parsed={parsed}
          onMap={(canonical) => {
            queue.resolve(item.id, "map_to_existing", {
              choice: canonical,
              canonical_name: canonical,
            });
            onClose();
          }}
          onKeep={() => {
            queue.resolve(item.id, "accept_as_new");
            onClose();
          }}
          onRename={(canonical) => {
            queue.resolve(item.id, "suggest_better", { canonical_name: canonical });
            onClose();
          }}
          onDismiss={() => {
            queue.resolve(item.id, "reject");
            onClose();
          }}
        />
      ) : inference.isInference ? (
        <div className="rinf-actions">
          <button
            type="button"
            className={`rinf-approve${inference.edited ? " correction" : ""}`}
            onClick={approveInference}
          >
            {inference.edited ? "approve correction" : "approve"}
          </button>
          <button
            type="button"
            className={`rinf-reject${armed === "inf-reject" ? " armed" : ""}`}
            onClick={rejectInference}
          >
            {armed === "inf-reject" ? "tap again — discard" : "reject — discard"}
          </button>
        </div>
      ) : (
        <>
          <h3 className="section-header">choose among proposals</h3>
          <div className="rproposals">
            {proposals.map((proposal) => {
              const key = `prop-${proposal.action}`;
              const isArmed = armed === key;
              return (
                <button
                  key={proposal.action}
                  type="button"
                  className={`rprop${proposal.destructive ? " rprop-destructive" : ""}${
                    isArmed ? " armed" : ""
                  }`}
                  onClick={() => choose(proposal)}
                >
                  <span className="rprop-label">
                    {isArmed ? "tap again — this is permanent" : proposal.label}
                  </span>
                  {!isArmed && proposal.detail !== null && (
                    <span className="rprop-detail">{proposal.detail}</span>
                  )}
                </button>
              );
            })}
          </div>
        </>
      )}
    </>
  );
};

/** The decided record for a new_predicate card — Direction C (docs/mocks/
 * decided-view-mockups.html): a before→after diff of the change the decision
 * made. Derived entirely from the resolution + payload — no re-ticking of the
 * offered rows (which all share one map_to_existing action and so all ticked). */
function DecidedNewPredicate({ item, parsed }: { item: ReviewItem; parsed: Parsed }) {
  const action = item.resolution?.action ?? null;
  const named = item.resolution?.payload.canonical_name;
  const canonical = typeof named === "string" ? named : null;
  const before = parsed.predicate ?? "";
  const subject = parsed.subject ?? "this";
  const value = parsed.value ?? parsed.statement ?? "?";

  let after = before;
  let verb = "Decided";
  let tone = "tone-muted";
  let nowSub = "";
  if (action === "map_to_existing" && canonical !== null) {
    after = canonical;
    verb = "Mapped to";
    tone = "tone-ok";
    nowSub = "an existing relation it now uses";
  } else if (action === "suggest_better" && canonical !== null) {
    after = canonical;
    verb = "Renamed to";
    tone = "tone-ok";
    nowSub = "your canonical — the fact now uses it";
  } else if (action === "accept_as_new") {
    verb = "Kept as new";
    tone = "tone-steel";
    nowSub = "registered as its own canonical relation";
  } else {
    verb = "Dismissed";
    nowSub = "left under its raw name — unchanged";
  }

  return (
    <div className={`rdc ${tone}`}>
      <div className="rdc-row rdc-was">
        <span className="rdc-lbl">was</span>
        <span className="rdc-pred was">{before}</span>
        <span className="rdc-sub">unrecognized — coined from the note</span>
      </div>
      <div className="rdc-mid">
        <span className="rdc-ln" />
        {after !== before ? `${verb} ${after}` : verb}
        <span className="rdc-ln" />
      </div>
      <div className="rdc-row rdc-now">
        <span className="rdc-lbl">now</span>
        <span className="rdc-pred now">{after}</span>
        <span className="rdc-sub">{nowSub}</span>
        <span className="rdc-edge">
          → <b>{`${subject}.${after} → ${value}`}</b>
        </span>
      </div>
    </div>
  );
}

function DecidedRecord({ item, parsed }: { item: ReviewItem; parsed: Parsed }) {
  const action = item.resolution?.action ?? null;
  const proposals = proposalsFor(parsed);
  if (action === "correct") {
    return (
      <p className="rdetail-cands">
        corrected — filed as a note; the pipeline applies your fix to the wiki.
      </p>
    );
  }
  // new_predicate states its outcome as a before→after diff (map/keep/rename/
  // dismiss), so it never re-ticks the offered rows.
  if (item.kind === "new_predicate") {
    return <DecidedNewPredicate item={item} parsed={parsed} />;
  }
  return (
    <>
      <h3 className="section-header">what was decided</h3>
      <div className="offered">
        {proposals.map((proposal) => {
          const chosen = proposal.action === action;
          const text =
            proposal.detail !== null ? `${proposal.label} — ${proposal.detail}` : proposal.label;
          return (
            <div key={proposal.action} className={`offered-row${chosen ? " chosen" : ""}`}>
              <span className="offered-mark">{chosen ? "✓" : ""}</span>
              <span>{text}</span>
            </div>
          );
        })}
        {action === "dismiss" && (
          <div className="offered-row">dismissed — skipped without a decision</div>
        )}
      </div>
    </>
  );
}
