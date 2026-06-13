// Entity page (docs/DESIGN.md "Analysis tab + entity pages" — the hub):
// centered node with kind/alias/domain meta, current facts as outbound
// edges, revision histories as vertical timeline rails (each dot a fact
// citing its note snippet, superseded ones muted), inbound edges, mentions.
// A slide-up tree layer like the note view: back chevron + swipe-down exit.

import { type TouchEvent, useEffect, useRef, useState } from "react";
import { EdgeValue, FactCitation, MarkedText, StatusChip } from "../analysis/bits";
import { edgePath, factSpan } from "../analysis/format";
import { type EntityOut, type EntityPredicate, type FactOut, api } from "../api/client";
import { TopBar } from "../components/TopBar";
import { EntityTypeIcon } from "../entities/kinds";
import { DOMAIN_COLOR, DOMAIN_TITLE } from "../notes/modes";
import type { SyncStatus } from "../notes/useNotes";

const SWIPE_DOWN_PX = 56;

type EntityState = { phase: "loading" } | { phase: "error" } | { phase: "done"; entity: EntityOut };

interface RailFactProps {
  fact: FactOut;
  onOpenEntity: (entityId: string) => void;
}

/** One dot on a predicate's timeline rail: value, span, source citation. */
function RailFact({ fact, onOpenEntity }: RailFactProps) {
  const [open, setOpen] = useState(false);
  const muted = fact.status === "superseded" || fact.status === "retracted";
  const toggle = () => setOpen((o) => !o);
  return (
    <li className={`rail-fact${muted ? " fact-superseded" : ""}`}>
      <span className="rail-dot" aria-hidden="true" />
      <div
        className="rail-body"
        // biome-ignore lint/a11y/useSemanticElements: the body hosts a nested object-entity link, which a real <button> cannot wrap.
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
          }
        }}
      >
        <span className="rail-value">
          <EdgeValue fact={fact} onOpenEntity={onOpenEntity} />
        </span>
        <span className="rail-span">
          {factSpan(fact)}
          <StatusChip status={fact.status} pinned={fact.pinned} />
        </span>
      </div>
      {open && <FactCitation fact={fact} extractor={null} />}
    </li>
  );
}

function PredicateBlock({
  pred,
  onOpenEntity,
}: {
  pred: EntityPredicate;
  onOpenEntity: (entityId: string) => void;
}) {
  const hasRail = pred.history.length > 1;
  const head = pred.current ?? pred.history[0];
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
      {hasRail && (
        <ul className="timeline-rail">
          {pred.history.map((fact) => (
            <RailFact key={fact.id} fact={fact} onOpenEntity={onOpenEntity} />
          ))}
        </ul>
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
          </>
        )}
      </div>
    </div>
  );
}
