// Entity page (docs/reference/DESIGN.md "Analysis tab + entity pages" — the hub):
// centered node with kind/alias/domain meta, then each property's CURRENT
// value as an outbound edge — the page is current-only so it stays a bounded
// few rows tall regardless of how much revision history exists. Prior once-true
// values sit behind a quiet "N earlier →" disclosure that opens the property's
// timeline in the shared <Sheet>; machine-retracted facts are excluded from the
// value view entirely. Inbound edges, mentions. A slide-up tree layer like the
// note view: back chevron + swipe-down exit.

import { type ChangeEvent, type TouchEvent, useEffect, useRef, useState } from "react";
import { EdgeValue, MarkedText, StatusChip } from "../analysis/bits";
import { edgePath, factSpan } from "../analysis/format";
import { type EntityOut, type EntityPredicate, type FactOut, api } from "../api/client";
import { TopBar } from "../components/TopBar";
import { EntityTypeIcon } from "../entities/kinds";
import { DOMAIN_COLOR, DOMAIN_TITLE } from "../notes/modes";
import type { SyncStatus } from "../notes/useNotes";
import { EntityHistorySheet } from "./EntityHistorySheet";

const SWIPE_DOWN_PX = 112;

type EntityState = { phase: "loading" } | { phase: "error" } | { phase: "done"; entity: EntityOut };

// Modalities that aren't a claim about the present, so they never floor as a
// current value (mirrors the backend CURRENT_ASSERTIONS — Wave 1, slice 2).
const IRREALIS = new Set(["hypothetical", "reported", "question", "expected"]);

/** The live value of a predicate. The backend `current` is the three-valued
 * head — an asserted value, or a negated retraction shown explicitly, never an
 * irrealis "maybe". Absent one, fall back to the newest fact that still belongs
 * on a current-only page: a contested pending_review value, but never a
 * machine-retracted error and never an irrealis assertion. A slot left with no
 * head drops out of the value view entirely. */
function predHead(pred: EntityPredicate): FactOut | undefined {
  return (
    pred.current ?? pred.history.find((f) => f.status !== "retracted" && !IRREALIS.has(f.assertion))
  );
}

/** Prior once-true values worth a history disclosure: superseded facts (never
 * the current head, never machine-retracted errors). */
function priorCount(pred: EntityPredicate, head: FactOut | undefined): number {
  return pred.history.filter((f) => f.id !== head?.id && f.status !== "retracted").length;
}

/** A predicate is FORMER when its live value is a closed interval (valid_to
 * set): the relationship has ended and nothing current replaced it — so it reads
 * as past, not present. An open head (valid_to null), including a pending one,
 * is current. */
function isFormer(pred: EntityPredicate): boolean {
  return predHead(pred)?.valid_to != null;
}

function PredicateBlock({
  pred,
  former = false,
  onOpenEntity,
  onOpenHistory,
}: {
  pred: EntityPredicate;
  // Renders the dimmed "previously" treatment with a vague tenure span.
  former?: boolean;
  onOpenEntity: (entityId: string) => void;
  onOpenHistory: (pred: EntityPredicate) => void;
}) {
  const head = predHead(pred);
  const earlier = priorCount(pred, head);
  const span = former && head ? factSpan(head) : "";
  return (
    <div className={`entity-pred${former ? " is-former" : ""}`}>
      <div className="pred-head">
        <span className="fact-edge">
          <span className="edge-path">{edgePath(pred.predicate, pred.qualifier)}</span>
          <span className="edge-arrow"> → </span>
          <span className="edge-value">
            {head ? <EdgeValue fact={head} onOpenEntity={onOpenEntity} /> : "—"}
          </span>
        </span>
        {span ? (
          <span className="fact-tenure-span">{span}</span>
        ) : (
          head && (
            <>
              {/* A negated current head is a live retraction — surface it
                  explicitly so the value doesn't read as still true. */}
              {head.assertion === "negated" && (
                <span className="fact-chip fact-chip-muted">not currently</span>
              )}
              <StatusChip status={head.status} pinned={head.pinned} />
            </>
          )
        )}
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
  // "Previously" (former relationships) is collapsed by default — calm leads
  // with what's true now.
  const [prevOpen, setPrevOpen] = useState(false);
  // The owner can attach a profile photo — the only owner-set bit of entity metadata; it's
  // copied onto the wiki article at upload and rebuild (not a claim, never machine-edited).
  const [photoBusy, setPhotoBusy] = useState(false);
  const photoRef = useRef<HTMLInputElement>(null);
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

  async function onPickPhoto(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // let the same file be re-picked after a failure
    if (!file) return;
    setPhotoBusy(true);
    try {
      await api.uploadEntityImage(entityId, file, file.name);
      const entity = await api.getEntity(entityId); // refetch so the new sha re-renders the img
      setState({ phase: "done", entity });
    } catch {
      // A transient upload failure just leaves the current photo; the owner can retry.
    } finally {
      setPhotoBusy(false);
    }
  }

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
              <button
                type="button"
                className="entity-photo"
                onClick={() => photoRef.current?.click()}
                aria-label={photoBusy ? "Uploading photo" : "Set profile photo"}
              >
                {state.entity.image_sha ? (
                  <img
                    className="entity-photo-img"
                    src={`/api/entities/${encodeURIComponent(entityId)}/image?v=${state.entity.image_sha}`}
                    alt={state.entity.canonical_name}
                  />
                ) : (
                  <EntityTypeIcon kind={state.entity.kind} size={48} />
                )}
                <span className="entity-photo-edit">{photoBusy ? "…" : "photo"}</span>
              </button>
              <input ref={photoRef} type="file" accept="image/*" hidden onChange={onPickPhoto} />
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

            {(() => {
              // Split by tense: a predicate whose live value still holds is
              // current; one whose live value has ended (a closed interval) is
              // former and drops into the collapsed "Previously" group, so a
              // relationship you've left never reads as present.
              // A slot with no current-eligible head (irrealis-only or
              // machine-retracted-only) drops out of the value view entirely.
              const shown = state.entity.predicates.filter((p) => predHead(p) !== undefined);
              const current = shown.filter((p) => !isFormer(p));
              const former = shown.filter(isFormer);
              // Key on the group's head fact (unique per block) — a set-valued
              // predicate yields one block per object, so the path alone collides.
              const keyOf = (p: EntityPredicate) =>
                p.history[0]?.id ?? edgePath(p.predicate, p.qualifier);
              return (
                <>
                  {current.length > 0 && (
                    <section>
                      <h3 className="section-header">Current</h3>
                      <div className="fact-card">
                        {current.map((pred) => (
                          <PredicateBlock
                            key={keyOf(pred)}
                            pred={pred}
                            onOpenEntity={onOpenEntity}
                            onOpenHistory={setHistoryPred}
                          />
                        ))}
                      </div>
                    </section>
                  )}

                  {former.length > 0 && (
                    <section>
                      <div className={`fact-card prev-group${prevOpen ? "" : " collapsed"}`}>
                        <button
                          type="button"
                          className="prev-head"
                          aria-expanded={prevOpen}
                          onClick={() => setPrevOpen((o) => !o)}
                        >
                          <span className="caret" aria-hidden="true">
                            ⌄
                          </span>
                          previously
                          <span className="count">
                            {former.length} former{current.length === 0 ? " · none current" : ""}
                          </span>
                        </button>
                        <div className="prev-body">
                          {former.map((pred) => (
                            <PredicateBlock
                              key={keyOf(pred)}
                              pred={pred}
                              former
                              onOpenEntity={onOpenEntity}
                              onOpenHistory={setHistoryPred}
                            />
                          ))}
                        </div>
                      </div>
                    </section>
                  )}
                </>
              );
            })()}

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
                        <span className="edge-path">—{edge.predicate}→</span>
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
