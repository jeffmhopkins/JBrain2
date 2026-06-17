import { useState } from "react";
import { edgePath } from "../../analysis/format";
import { matchBand } from "../payload";
import type { ReviewBlock } from "./types";

// The modality vocabulary (the fact's `assertion`), mirroring the backend
// ASSERTIONS set. `asserted` is the calm default; the rest let the owner correct
// a mis-read stance ("this was hypothetical", "this is a negation").
const MODALITIES = ["asserted", "negated", "hypothetical", "reported", "question", "expected"];

/** The proposed-fact panel for a low-confidence inference (docs/mocks/
 * review-edit-predicate-and-value): the `predicate → value` edge it would write,
 * rendered as the entity page does, with BOTH sides editable in place. The
 * predicate is a chip→weighted picker (the canonicals nearest the proposed
 * relation, ranked by similarity, plus free entry); the value is a chip→input,
 * or the members of a typed (closed-enum) predicate as chips. The matching
 * approve button lives in the action block, sharing this edit state through the
 * context, so editing either side flips approve → approve correction.
 * Self-gates unless this is an inference. */
export const ClaimInference: ReviewBlock = ({ ctx }) => {
  const { parsed, lane, inference } = ctx;
  if (!inference.editable) return null;
  const {
    originalValue,
    editValue,
    setEditValue,
    editingValue,
    setEditingValue,
    valueEdited,
    originalPredicate,
    editPredicate,
    setEditPredicate,
    editingPredicate,
    setEditingPredicate,
    predicateEdited,
    predicateSuggestions,
    originalModality,
    editModality,
    setEditModality,
    modalityEdited,
  } = inference;
  const pending = lane === "pending";

  return (
    <div className="rproposed" aria-label="proposed fact">
      <span className="rdiff-lbl">
        proposed fact
        {parsed.enumValues.length > 0 && <span className="rinf-typed">closed set</span>}
      </span>
      <span className="fact-edge">
        {/* ── predicate (the relation) ── */}
        {pending && editingPredicate ? null : pending ? (
          <button
            type="button"
            className={`rinf-chip pred${predicateEdited ? " edited" : ""}`}
            onClick={() => setEditingPredicate(true)}
          >
            <span className="rinf-val">{edgePath(editPredicate, parsed.qualifier)}</span>
            <span className="rinf-pen" aria-hidden="true">
              ✎ edit
            </span>
          </button>
        ) : (
          <span className="edge-path">{edgePath(editPredicate, parsed.qualifier)}</span>
        )}
        {!(pending && editingPredicate) && <span className="edge-arrow"> → </span>}

        {/* ── value ── */}
        {!pending || parsed.enumValues.length > 0 ? (
          <span className={`edge-value${valueEdited ? " rinf-edited" : ""}`}>
            {pending ? editValue : originalValue}
          </span>
        ) : editingValue ? (
          <input
            className="rinf-input"
            ref={(el) => el?.focus()}
            value={editValue}
            aria-label="corrected value"
            onChange={(e) => setEditValue(e.target.value)}
            onBlur={() => setEditingValue(false)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setEditingValue(false);
            }}
          />
        ) : (
          <button
            type="button"
            className={`rinf-chip${valueEdited ? " edited" : ""}`}
            onClick={() => setEditingValue(true)}
          >
            <span className="rinf-val">{editValue}</span>
            <span className="rinf-pen" aria-hidden="true">
              ✎ edit
            </span>
          </button>
        )}
      </span>

      {/* ── predicate picker: weighted candidates + manual entry ── */}
      {pending && editingPredicate && (
        <PredicatePicker
          original={originalPredicate}
          qualifier={parsed.qualifier}
          suggestions={predicateSuggestions}
          onPick={(name) => {
            setEditPredicate(name);
            setEditingPredicate(false);
          }}
          onDone={() => setEditingPredicate(false)}
        />
      )}

      {pending && parsed.enumValues.length > 0 && (
        <div className="rinf-enum">
          {parsed.enumValues.map((v) => (
            <button
              key={v}
              type="button"
              className={`rinf-enum-chip${editValue === v ? " on" : ""}`}
              aria-pressed={editValue === v}
              onClick={() => setEditValue(v)}
            >
              {v}
            </button>
          ))}
        </div>
      )}
      {/* ── modality (the assertion stance) ── */}
      {pending && (
        <ModalityField value={editModality} edited={modalityEdited} onPick={setEditModality} />
      )}

      {pending && (inference.edited || parsed.accept !== null) && (
        <p className={`rinf-status${inference.edited ? " edit" : ""}`}>
          {inference.edited ? (
            <>
              correcting{" "}
              {predicateEdited && (
                <>
                  relation <s>{edgePath(originalPredicate, parsed.qualifier)}</s> →{" "}
                  <b>{edgePath(editPredicate.trim(), parsed.qualifier)}</b>
                </>
              )}
              {predicateEdited && valueEdited && " · "}
              {valueEdited && (
                <>
                  value <s>{originalValue}</s> → <b>{editValue.trim()}</b>
                </>
              )}
              {(predicateEdited || valueEdited) && modalityEdited && " · "}
              {modalityEdited && (
                <>
                  modality <s>{originalModality}</s> → <b>{editModality}</b>
                </>
              )}{" "}
              — filed as a correction note; the pipeline applies it, so the wiki stays
              machine-written.
            </>
          ) : (
            parsed.accept
          )}
        </p>
      )}
    </div>
  );
};

/** The modality control: a calm chip showing the fact's current stance
 * (asserted by default), which expands to the closed set of assertions so the
 * owner can correct a mis-read one — "this was hypothetical", "this is a
 * negation". Like the predicate/value sides, a change flips approve → approve
 * correction and is filed as a correction note (never a direct wiki edit). */
function ModalityField({
  value,
  edited,
  onPick,
}: {
  value: string;
  edited: boolean;
  onPick: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rinf-field" aria-label="modality">
      <span className="rinf-field-lbl">modality</span>
      {open ? (
        <div className="rinf-enum">
          {MODALITIES.map((m) => (
            <button
              key={m}
              type="button"
              className={`rinf-enum-chip${value === m ? " on" : ""}`}
              aria-pressed={value === m}
              onClick={() => {
                onPick(m);
                setOpen(false);
              }}
            >
              {m}
            </button>
          ))}
        </div>
      ) : (
        <button
          type="button"
          className={`rinf-chip${edited ? " edited" : ""}`}
          onClick={() => setOpen(true)}
        >
          <span className="rinf-val">{value}</span>
          <span className="rinf-pen" aria-hidden="true">
            ✎ edit
          </span>
        </button>
      )}
    </div>
  );
}

/** The relation picker: the canonicals nearest the proposed predicate (weighted
 * by similarity, strongest first, the current one marked at the top), plus a
 * search box that both filters the list and — via the "use …" row — coins a new
 * relation from free text. Picking a row, the coin row, or pressing Enter on a
 * non-empty search commits it. */
function PredicatePicker({
  original,
  qualifier,
  suggestions,
  onPick,
  onDone,
}: {
  original: string;
  qualifier: string | null;
  suggestions: { name: string; score: number }[];
  onPick: (name: string) => void;
  onDone: () => void;
}) {
  const [search, setSearch] = useState("");
  const q = search.trim().toLowerCase();
  // The current relation heads the list (one tap to revert), then the weighted
  // candidates, deduped against it. The search filters by substring.
  const ranked = [
    { name: original, score: null as number | null, current: true },
    ...suggestions
      .filter((s) => s.name !== original)
      .map((s) => ({ name: s.name, score: s.score, current: false })),
  ].filter((r) => q.length === 0 || r.name.toLowerCase().includes(q));
  // A free-text relation not already on offer — coin it as-is.
  const canCoin = q.length > 0 && !ranked.some((r) => r.name.toLowerCase() === q);

  return (
    <div className="rinf-pred-picker">
      <div className="rinf-pred-head">
        {suggestions.length > 0
          ? "relations ranked by fit — pick one, or type to coin a new one"
          : "type a relation to coin a new one"}
        <button type="button" className="rinf-pred-done" onClick={onDone}>
          done
        </button>
      </div>
      <input
        className="rinf-input rinf-pred-search"
        ref={(el) => el?.focus()}
        value={search}
        aria-label="search or type a relation"
        placeholder="search or type a relation…"
        onChange={(e) => setSearch(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && q.length > 0) onPick(search.trim());
        }}
      />
      {(ranked.length > 0 || canCoin) && (
        <div className="rinf-pred-list">
          {ranked.map((r) => {
            const band = r.score !== null ? matchBand(r.score) : null;
            return (
              <button
                key={r.name}
                type="button"
                className={`rinf-pred-opt${r.current ? " current" : ""}`}
                onClick={() => onPick(r.name)}
              >
                <span className="rinf-pred-name">{edgePath(r.name, qualifier)}</span>
                {r.current ? (
                  <span className="rinf-pred-cur">current</span>
                ) : (
                  band && (
                    <span className={`rnp-match ${band.cls}`}>
                      <span className="rnp-bar">
                        <i style={{ width: `${Math.round((r.score ?? 0) * 100)}%` }} />
                      </span>
                      {band.label}
                    </span>
                  )
                )}
              </button>
            );
          })}
          {canCoin && (
            <button
              type="button"
              className="rinf-pred-opt coin"
              onClick={() => onPick(search.trim())}
            >
              <span className="rinf-pred-name">{search.trim()}</span>
              <span className="rinf-pred-cur">use as a new relation</span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}
