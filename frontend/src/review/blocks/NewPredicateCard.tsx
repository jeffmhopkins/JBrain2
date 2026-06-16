import { useState } from "react";
import { type Parsed, matchBand } from "../payload";

/** new_predicate card — Direction A (docs/mocks/new-predicate-mockups.html): the
 * triggering fact as context, then the candidate canonicals as a ranked list —
 * each with a match-strength bar and a preview of the edge mapping would write —
 * with keep-as-new, rename, and dismiss grouped below as the fallbacks. */
export function NewPredicateCard({
  parsed,
  onMap,
  onKeep,
  onRename,
  onDismiss,
}: {
  parsed: Parsed;
  onMap: (name: string) => void;
  onKeep: () => void;
  onRename: (name: string) => void;
  onDismiss: () => void;
}) {
  const [name, setName] = useState("");
  const subject = parsed.subject ?? "this";
  const value = parsed.value ?? parsed.statement ?? "?";
  const pred = parsed.predicate ?? "";
  return (
    <div className="rnp">
      <div className="rnp-context">
        <span className="rnp-lbl">unrecognized relation · committed under its raw name</span>
        <span className="rnp-edge">
          <span className="rnp-subj">{subject}</span>
          <span className="rnp-bracket"> —[</span>
          <span className="rnp-unknown">{pred}</span>
          <span className="rnp-bracket">]→ </span>
          <span className="rnp-val">{value}</span>
        </span>
        {parsed.statement !== null && <span className="rnp-quote">“{parsed.statement}”</span>}
      </div>

      {parsed.suggestions.length > 0 && (
        <>
          <h3 className="section-header">map it to a known relation</h3>
          <div className="rnp-opts">
            {parsed.suggestions.map((s, i) => {
              const band = matchBand(s.score);
              return (
                <button
                  key={s.name}
                  type="button"
                  className={`rnp-opt${i === 0 ? " best" : ""}`}
                  onClick={() => onMap(s.name)}
                >
                  <span className="rnp-opt-main">
                    <span className="rnp-opt-top">
                      <span className="rnp-opt-name">{s.name}</span>
                      {i === 0 && <span className="rnp-tag-best">best match</span>}
                      <span className={`rnp-match ${band.cls}`}>
                        <span className="rnp-bar">
                          <i style={{ width: `${Math.round(s.score * 100)}%` }} />
                        </span>
                        {band.label}
                      </span>
                    </span>
                    <span className="rnp-opt-prev">
                      → <b>{`${subject}.${s.name} → ${value}`}</b>
                    </span>
                  </span>
                  <span className="rnp-opt-go" aria-hidden="true">
                    ›
                  </span>
                </button>
              );
            })}
          </div>
        </>
      )}

      <div className="rnp-divider">or</div>

      <button type="button" className="rnp-minor" onClick={onKeep}>
        <span className="rnp-minor-l">
          Keep <code className="rnp-code">{pred}</code> as a new relation
        </span>
        <span className="rnp-minor-d">registers it as its own canonical predicate, used as-is</span>
      </button>

      <div className="rnp-rename">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="rename the relation"
          placeholder="…or rename it, e.g. spouse"
        />
        <button
          type="button"
          disabled={name.trim().length === 0}
          onClick={() => onRename(name.trim())}
        >
          use
        </button>
      </div>

      <button type="button" className="rnp-minor danger" onClick={onDismiss}>
        <span className="rnp-minor-l">Dismiss</span>
        <span className="rnp-minor-d">leave the fact under its raw name, clear this card</span>
      </button>
    </div>
  );
}
