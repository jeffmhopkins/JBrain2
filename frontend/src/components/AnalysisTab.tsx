// Note view Analysis tab (docs/DESIGN.md "Analysis tab + entity pages" —
// graph-forward): facts render as literal property-graph edges grouped by
// subject node; subject headers double as entity navigation; tapping a fact
// expands its citation back to the highlighted source words.

import { useEffect, useState } from "react";
import { FactCitation, KindBadge, StatusChip } from "../analysis/bits";
import { edgePath, factValue, fmtConfidence, fmtTemporal } from "../analysis/format";
import { type AnalysisEntity, type FactOut, type NoteAnalysis, api } from "../api/client";

type AnalysisState =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "done"; analysis: NoteAnalysis };

interface SubjectGroup {
  entity: AnalysisEntity;
  facts: FactOut[];
}

/** Group facts by subject, in order of first appearance. */
function groupBySubject(analysis: NoteAnalysis): SubjectGroup[] {
  const byId = new Map(analysis.entities.map((e) => [e.id, e]));
  const groups: SubjectGroup[] = [];
  for (const fact of analysis.facts) {
    const last = groups.find((g) => g.entity.id === fact.entity_id);
    if (last) {
      last.facts.push(fact);
      continue;
    }
    const entity = byId.get(fact.entity_id) ?? {
      id: fact.entity_id,
      kind: "",
      name: fact.entity_name,
      status: "active",
    };
    groups.push({ entity, facts: [fact] });
  }
  return groups;
}

interface FactRowProps {
  fact: FactOut;
  extractor: string | null;
}

function FactRow({ fact, extractor }: FactRowProps) {
  const [open, setOpen] = useState(false);
  return (
    <div className="fact-row-wrap">
      <button
        type="button"
        className="fact-row"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="fact-edge">
          <span className="edge-path">{edgePath(fact.predicate, fact.qualifier)}</span>
          <span className="edge-arrow"> → </span>
          <span className="edge-value">{factValue(fact)}</span>
        </span>
        <span className="fact-meta">
          <KindBadge kind={fact.kind} />
          <StatusChip status={fact.status} pinned={fact.pinned} />
          <span className="fact-conf">{fmtConfidence(fact.confidence)}</span>
        </span>
      </button>
      {open && <FactCitation fact={fact} extractor={extractor} />}
    </div>
  );
}

interface AnalysisTabProps {
  /** Server note id; null for unsynced outbox rows (nothing to analyze yet). */
  noteId: string | null;
  onOpenEntity: (entityId: string) => void;
}

export function AnalysisTab({ noteId, onOpenEntity }: AnalysisTabProps) {
  const [state, setState] = useState<AnalysisState>({ phase: "loading" });

  useEffect(() => {
    if (noteId === null) return;
    let stale = false;
    api
      .noteAnalysis(noteId)
      .then((analysis) => {
        if (!stale) setState({ phase: "done", analysis });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [noteId]);

  if (noteId === null || (state.phase === "done" && state.analysis.analyzed_at === null)) {
    return <p className="analysis-quiet">analysis runs after indexing — nothing here yet.</p>;
  }
  if (state.phase === "loading") {
    return <p className="analysis-quiet">loading analysis…</p>;
  }
  if (state.phase === "error") {
    return <p className="analysis-quiet">couldn't load analysis — reopen to retry.</p>;
  }

  const { analysis } = state;
  const groups = groupBySubject(analysis);

  return (
    <div className="analysis-tab">
      {analysis.title !== null && <h2 className="analysis-title">{analysis.title}</h2>}
      {analysis.tags.length > 0 && (
        <div className="tag-row">
          {analysis.tags.map((tag) => (
            <span key={tag} className="tag-pill">
              {tag}
            </span>
          ))}
        </div>
      )}

      {groups.map((group) => (
        <section key={group.entity.id} className="subject-group">
          <button
            type="button"
            className="entity-chip"
            onClick={() => onOpenEntity(group.entity.id)}
          >
            <span className="entity-chip-name">{group.entity.name}</span>
            {group.entity.kind !== "" && (
              <span className="entity-chip-kind">{group.entity.kind.toLowerCase()}</span>
            )}
            {group.entity.status === "provisional" && (
              <span className="fact-chip fact-chip-muted">provisional</span>
            )}
          </button>
          <div className="fact-card">
            {group.facts.map((fact) => (
              <FactRow key={fact.id} fact={fact} extractor={analysis.extractor} />
            ))}
          </div>
        </section>
      ))}

      {analysis.temporal_tokens.length > 0 && (
        <section>
          <h3 className="section-header">Dates</h3>
          <div className="token-row">
            {analysis.temporal_tokens.map((token) => (
              <span key={token.id} className="token-chip">
                {fmtTemporal(token.resolved_start, token.temporal_precision)}
                {token.resolved_end !== null &&
                  ` → ${fmtTemporal(token.resolved_end, token.temporal_precision)}`}
                <span className="token-phrase">“{token.surface_phrase}”</span>
              </span>
            ))}
          </div>
        </section>
      )}

      <p className="provenance-foot">
        {/* analyzed_at is a real instant, not a calendar date: keep it local */}
        analyzed {fmtTemporal(analysis.analyzed_at, "instant")}
        {analysis.extractor !== null && ` · ${analysis.extractor}`}
      </p>
    </div>
  );
}
