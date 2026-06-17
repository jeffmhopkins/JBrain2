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
  // The edge clause covers whichever of relation/value changed. Modality (the
  // assertion stance) is orthogonal, so it gets its own sentence — and may be
  // the only edit, hence the standalone branch.
  const clauses: string[] = [];
  if (inf.predicateEdited && inf.valueEdited) {
    clauses.push(`This should be ${toPath} → ${value}, not ${fromPath} → ${inf.originalValue}.`);
  } else if (inf.predicateEdited) {
    clauses.push(`The relation should be ${toPath}, not ${fromPath} (value ${inf.originalValue}).`);
  } else if (inf.valueEdited) {
    clauses.push(`The value for ${fromPath} should be ${value}, not ${inf.originalValue}.`);
  }
  if (inf.modalityEdited) {
    clauses.push(
      `This is ${inf.editModality}, not ${inf.originalModality} (${toPath} → ${value}).`,
    );
  }
  return `${lead}\n\n${clauses.join(" ")}`;
}

/** The polymorphic decision block: the correction-note composer, then the
 * controls a pending item offers (new_predicate map/keep/rename/dismiss, an
 * inference's approve/reject, a conflict/collision's choose-among-proposals that
 * becomes apply/discard once edited in place, or the generic
 * choose-among-proposals), or — in the decided lane — the record of what was
 * decided. */
export const Action: ReviewBlock = ({ ctx }) => {
  const { item, parsed, lane, queue, armed, tap, onClose, onAdvance, inference, composing, draft } =
    ctx;
  const proposals = proposalsFor(parsed);

  // Resolve via a proposal the card advertises (data, not a per-kind branch).
  // Fact-bearing cards advance to the next item (triage flow); the rest return
  // to the list. A destructive proposal arms a confirm-tap first.
  function choose(proposal: Proposal, advance: boolean) {
    const key = `prop-${proposal.action}`;
    if (proposal.destructive && !tap(key)) return;
    queue.resolve(item.id, proposal.action, {
      choice: proposal.label,
      ...(proposal.payload ?? {}),
    });
    advance ? onAdvance() : onClose();
  }

  function fileCorrection() {
    if (draft.trim().length === 0) return;
    queue.correct(item.id, draft.trim());
    onClose();
  }

  // An edited proposed fact (any editable card — inference, conflict, collision)
  // files a correction note instead of a verbatim pick: the #7 channel, never a
  // hand-written fact. Discard reverts the edit so the card's proposals return.
  function approveCorrection() {
    queue.correct(item.id, inferenceCorrection(item, parsed, inference));
    onAdvance();
  }
  function discardEdit() {
    inference.setEditValue(inference.originalValue);
    inference.setEditPredicate(inference.originalPredicate);
    inference.setEditModality(inference.originalModality);
    inference.setEditingValue(false);
    inference.setEditingPredicate(false);
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
      {/* new_predicate edits a predicate MAPPING (not a value), so it keeps its
          own control. Every other fact-bearing card shares one path: edit the
          proposed fact -> approve correction; else pick among its proposals. */}
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
      ) : inference.editable && inference.edited ? (
        <div className="rinf-actions">
          <button type="button" className="rinf-approve correction" onClick={approveCorrection}>
            approve correction
          </button>
          <button type="button" className="rinf-reject" onClick={discardEdit}>
            discard edit
          </button>
        </div>
      ) : (
        <Proposals
          proposals={proposals}
          armed={armed}
          onChoose={(p) => choose(p, inference.editable)}
        />
      )}
    </>
  );
};

/** The proposals a pending card advertises, as stacked buttons. One renderer for
 * every kind that picks among values (a conflict's accept_a/accept_b, an
 * inference's approve/reject, a merge's accept/reject) — the difference is the
 * data the card carries, not the control. A destructive proposal shows its
 * armed confirm copy. */
function Proposals({
  proposals,
  armed,
  onChoose,
}: {
  proposals: Proposal[];
  armed: string | null;
  onChoose: (proposal: Proposal) => void;
}) {
  return (
    <>
      <h3 className="section-header">choose among proposals</h3>
      <div className="rproposals">
        {proposals.map((proposal) => {
          const isArmed = armed === `prop-${proposal.action}`;
          return (
            <button
              key={proposal.action}
              type="button"
              className={`rprop${proposal.destructive ? " rprop-destructive" : ""}${
                isArmed ? " armed" : ""
              }`}
              onClick={() => onChoose(proposal)}
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
  );
}

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
