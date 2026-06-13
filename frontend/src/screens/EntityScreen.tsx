// Entity page (docs/DESIGN.md "Analysis tab + entity pages" — the hub):
// centered node with kind/alias/domain meta, then each property's CURRENT
// value as an outbound edge — the page is current-only so it stays a bounded
// few rows tall regardless of how much revision history exists. Prior once-true
// values sit behind a quiet "N earlier →" disclosure that opens the property's
// timeline in the shared <Sheet>; machine-retracted facts are excluded from the
// value view entirely. Inbound edges, mentions. A slide-up tree layer like the
// note view: back chevron + swipe-down exit.

import { type TouchEvent, useEffect, useRef, useState } from "react";
import { EdgeValue, MarkedText, StatusChip } from "../analysis/bits";
import { edgePath } from "../analysis/format";
import { type EntityOut, type EntityPredicate, type FactOut, api } from "../api/client";
import { TopBar } from "../components/TopBar";
import { EntityTypeIcon } from "../entities/kinds";
import { DOMAIN_COLOR, DOMAIN_TITLE } from "../notes/modes";
import type { SyncStatus } from "../notes/useNotes";
import { EntityHistorySheet } from "./EntityHistorySheet";

const SWIPE_DOWN_PX = 56;

type EntityState = { phase: "loading" } | { phase: "error" } | { phase: "done"; entity: EntityOut };

/** The live value of a predicate: the active fact, else the newest non-retracted
 * one (a property whose only fact is pending_review still shows it). */
function predHead(pred: EntityPredicate): FactOut | undefined {
  return pred.current ?? pred.history.find((f) => f.status !== "retracted") ?? pred.history[0];
}

/** Prior once-true values worth a history disclosure: superseded facts (never
 * the current head, never machine-retracted errors). */
function priorCount(pred: EntityPredicate, head: FactOut | undefined): number {
  return pred.history.filter((f) => f.id !== head?.id && f.status !== "retracted").length;
}

function PredicateBlock({
  pred,
  onOpenEntity,
  onOpenHistory,
}: {
  pred: EntityPredicate;
  onOpenEntity: (entityId: string) => void;
  onOpenHistory: (pred: EntityPredicate) => void;
}) {
  const head = predHead(pred);
  const earlier = priorCount(pred, head);
  return (
    <div className="entity-pred">
      <div className="pred-head">
        <span className="fact-edge">
          <span className="edge-path">{edgePath(pred.predicate, pred.qualifier)}</span>
          <span className="edge-arrow"> → </span>
          <span className="edge-value">
            {head ? <EdgeValue fact={head} onOpenEntity={onOpenEntity} /> : "—"}
          </span>
        </span>
        {head && <StatusChip status={head.status} pinned={head.pinned} />}
      </div>
      {earlier > 0 && (
        <button type="button" className="pred-history-toggle" onClick={() => onOpenHistory(pred)}>
          {earlier} earlier →
        </button>
      )}
    </div>
  );
}

interface EntityScreenProps {
  entityId: string;
  syncStatus: SyncStatus;
  onClose: () => void;
  /** Swap this layer to another entity (inbound-edge chips). */
  onOpenEntity: (entityId: string) => void;
  /** Open the cited note view, if the note is reachable. */
  onOpenNote: (noteId: string) => void;
}

export function EntityScreen({
  entityId,
  syncStatus,
  onClose,
  onOpenEntity,
  onOpenNote,
}: EntityScreenProps) {
  const [state, setState] = useState<EntityState>({ phase: "loading" });
  const [historyPred, setHistoryPred] = useState<EntityPredicate | null>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const swipeStart = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    let stale = false;
    setState({ phase: "loading" });
    api
      .getEntity(entityId)
      .then((entity) => {
        if (!stale) setState({ phase: "done", entity });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [entityId]);

  // Swipe-down at scroll-top climbs back, same as every card layer.
  function onTouchStart(event: TouchEvent) {
    if ((scrollerRef.current?.scrollTop ?? 0) > 4) {
      swipeStart.current = null;
      return;
    }
    const t = event.touches[0];
    swipeStart.current = t ? { x: t.clientX, y: t.clientY } : null;
  }

  function onTouchMove(event: TouchEvent) {
    const start = swipeStart.current;
    const t = event.touches[0];
    if (!start || !t) return;
    const dy = t.clientY - start.y;
    const dx = Math.abs(t.clientX - start.x);
    if (dy > SWIPE_DOWN_PX && dy > dx * 2) {
      swipeStart.current = null;
      onClose();
    }
  }

  return (
    <div
      className="subscreen subscreen-entity"
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
    >
      <TopBar title="Entity" onBack={onClose} syncStatus={syncStatus} onBolt={onClose} />
      <div className="screen-body entity-view" ref={scrollerRef}>
        {state.phase === "loading" && <p className="analysis-quiet">loading entity…</p>}
        {state.phase === "error" && (
          <p className="analysis-quiet">couldn't load this entity — reopen to retry.</p>
        )}
        {state.phase === "done" && (
          <>
            <header className="entity-hub">
              <EntityTypeIcon kind={state.entity.kind} size={48} />
              <h2 className="entity-name">{state.entity.canonical_name}</h2>
              <p className="entity-kind-row">
                <span>{state.entity.kind.toLowerCase()}</span>
                {state.entity.status === "provisional" && (
                  <span className="fact-chip fact-chip-muted">provisional</span>
                )}
              </p>
              {state.entity.aliases.length > 0 && (
                <p className="entity-aliases">
                  also {state.entity.aliases.map((a) => `“${a}”`).join(", ")}
                </p>
              )}
              <p className="entity-domain">
                <span
                  className="domain-dot"
                  style={{ background: DOMAIN_COLOR[state.entity.domain] ?? "var(--steel)" }}
                />
                {(DOMAIN_TITLE[state.entity.domain] ?? state.entity.domain).toLowerCase()}
              </p>
            </header>

            {state.entity.predicates.length > 0 && (
              <section>
                <h3 className="section-header">Current</h3>
                <div className="fact-card">
                  {state.entity.predicates.map((pred) => (
                    <PredicateBlock
                      key={edgePath(pred.predicate, pred.qualifier)}
                      pred={pred}
                      onOpenEntity={onOpenEntity}
                      onOpenHistory={setHistoryPred}
                    />
                  ))}
                </div>
              </section>
            )}

            {state.entity.inbound.length > 0 && (
              <section>
                <h3 className="section-header">Linked from</h3>
                <div className="fact-card">
                  {state.entity.inbound.map((edge) => (
                    <div key={`${edge.entity_id}-${edge.predicate}`} className="inbound-row">
                      <button
                        type="button"
                        className="edge-object inbound-source"
                        onClick={() => onOpenEntity(edge.entity_id)}
                      >
                        {edge.name}
                      </button>
                      <span className="inbound-edge">
                        <span className="edge-path">—{edge.predicate}→</span> {edge.statement}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {state.entity.mentions.length > 0 && (
              <section>
                <h3 className="section-header">Mentions</h3>
                <div className="fact-card">
                  {state.entity.mentions.map((mention) => (
                    <button
                      key={`${mention.note_id}-${mention.created_at}`}
                      type="button"
                      className="mention-row"
                      onClick={() => onOpenNote(mention.note_id)}
                    >
                      <span className="mention-snippet">
                        <MarkedText text={mention.snippet} />
                      </span>
                      <span className="mention-date">
                        {new Date(mention.created_at).toLocaleDateString(undefined, {
                          month: "short",
                          day: "numeric",
                          year: "numeric",
                        })}
                      </span>
                    </button>
                  ))}
                </div>
              </section>
            )}

            {historyPred && (
              <EntityHistorySheet
                pred={historyPred}
                onClose={() => setHistoryPred(null)}
                onOpenEntity={onOpenEntity}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}
