"""Note-deletion purge: hard-delete everything derived from a note.

docs/reference/ANALYSIS.md doctrine says nothing derived is ever deleted — the
supersession chain is the revision history. This module is the one
sanctioned exception [decided]: notes are the sole sources of truth, so
deleting a note is a privacy promise that everything derived from it goes
too — facts, entity mentions, temporal tokens, review items in ANY status
(resolved history carries frozen snippets of the note's text), the
note_analysis row, provisional entities no surviving note references, and
the note's agent ingest conversations (whose transcripts hold the body).
The note's clarification blocks (D6) go too: they are not derived — the owner
typed them — but the promise is about the note, and they are part of its text.
The note row itself stays soft-deleted (the settled Phase 2 behavior); only
the derived graph purges, and the pipeline skips deleted notes, so nothing
re-creates these artifacts afterward.

Runs on the caller's session, inside the SAME transaction as the soft
delete, so a failed purge never leaves a half-purged graph. This is a
sibling of the review-reopen effects-unwind (analysis/repo.py): both repair
the graph when a write that shaped it is taken back — reopen replays
recorded effects, the purge re-derives chain repairs from what survives.

`purge_note_artifacts(keep_pinned=True)` is the SECOND caller of this destructive
half: the corpus rebuild sweep (analysis/rebuild.py), which re-derives from notes
that still exist. Its five exemptions are on that keyword's docstring — a rebuild
is not a deletion promise, so pinned decisions, every review item that is not still
open (and the facts it names), agent episodes, note conversations and the note's
own clarification blocks all survive it.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import bindparam, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from jbrain.analysis.appointment_projection import project_appointments
from jbrain.analysis.emr_projection import project_emr
from jbrain.analysis.geofence_projection import project_place_geofences
from jbrain.models.agent import AgentEpisode, AgentEpisodeRef, AgentSession
from jbrain.models.analysis import (
    Entity,
    EntityDistinction,
    EntityMention,
    Fact,
    NoteAnalysis,
    TemporalToken,
)
from jbrain.models.note_conversation import NoteConversation
from jbrain.models.notes import NoteClarification

log = structlog.get_logger()


def chain_repair_target(
    start: uuid.UUID | None, doomed_links: dict[uuid.UUID, uuid.UUID | None]
) -> uuid.UUID | None:
    """First transitive supersessor of `start` that is NOT doomed; None when
    the chain dies inside the doomed set.

    `doomed_links` maps each doomed fact to its own superseded_by, so a
    survivor pointing into the doomed set can re-attach to whatever surviving
    fact sits deeper in the chain — multiple consecutive doomed links are
    walked through. A cycle would mean corrupted chain data; bail to None
    (treat as chain-dead) rather than loop forever.
    """
    seen: set[uuid.UUID] = set()
    current = start
    while current is not None and current in doomed_links:
        if current in seen:
            return None
        seen.add(current)
        current = doomed_links[current]
    return current


@dataclass(frozen=True)
class PurgeCounts:
    """What one note's purge removed and spared — the rebuild sweep's per-note
    progress unit. `kept` is always 0 for a privacy delete: nothing is spared."""

    purged: int
    kept: int


# Supersession chains are short (a value revised a handful of times); the cap only
# stops a corrupted cyclic chain from spinning the recursive walk forever. Hitting it
# UNDER-spares, which is the data-loss direction, so the walk logs rather than
# truncating silently.
_CHAIN_DEPTH_CAP = 32

# The review-item statuses a REBUILD retires. Open cards are re-derivable noise, so the
# sweep drops them and lets the re-run re-file whatever still applies. Every OTHER status
# — `resolved`, `dismissed`, `deferred` (migration 0024) — names a row that SURVIVES the
# purge, and the spare set below is derived from exactly that complement rather than from
# its own list of "settled" statuses. Deriving is the point: a card that outlives the
# rebuild must outlive it holding live facts, whatever status parked it there, and
# `deferred` is the case that proves it — un-parking returns the card to `open`, and a
# card re-queued against two deleted rows is unservable.
_REBUILD_PURGED_STATUSES = ("open",)

# Every payload key by which ANY review-item kind names a FACT, derived kind by kind from
# the ~15 the `review_items_kind_check` allowlist admits (migration 0120) — not from the
# subset that happened to appear in a bug report. Both the spare set and
# `delete_review_items` read this, so the two can never disagree about what a card points
# at, and a new kind that reuses one of these keys is covered on the day it is added:
#
#   fact_a + fact_b : fact_conflict, attribute_collision, low_confidence — exactly the
#                     three `review_kind`s `decide()` emits (analysis/supersession.py),
#                     filed through the two `ReviewItem(kind=decision.review_kind)` sites
#                     in analysis/pipeline.py (the primary edge and its derived inverse),
#                     which share one payload shape.
#   source_fact_id  : inverse_proposal. NOT a `decide()` review_kind — it has its own
#                     filer (pipeline.py `_write_inverse`), the cross-subject firewall arm
#                     that refuses to auto-write a reciprocal onto another subject's
#                     stream and proposes it instead, naming the PRIMARY fact it mirrors.
#                     `resolve_review` has no branch for the kind, so the card only ever
#                     dismisses/defers/corrects and `_reverse_effects` never replays
#                     against this key — the harm of dropping it is a dead provenance
#                     pointer, not a broken undo. Spared regardless: the whole point of
#                     this constant is that the mapping is complete, not that each entry
#                     earns its place by current blast radius.
#   fact_id         : low_confidence_inference (pipeline.py — the held pending_review
#                     row; REJECT retracts it and does NOT pin, so it reaches neither the
#                     pin walk nor a fact_a/fact_b lookup, and a two-key spare set
#                     silently deleted it), domain_promotion (pipeline.py — accept pins
#                     the fact and reject writes nothing, so the pin walk alone already
#                     covers it; spared here anyway because sharing the key costs nothing
#                     and stops correctness depending on repo.py's current verbs),
#                     wiki_stale_claim (wiki/lint.py — the superseded fact an article
#                     still frames as current).
#   no fact key     : merge_proposal (entity_a/entity_b), confirm_entity (entity_id),
#                     ambiguous_mention (name + candidate entity_ids), wiki_contradiction
#                     (entity_ids), extraction_truncated (note_id only), the EMR-ingest
#                     arm of low_confidence (note_id + subkind + attachment/analyte key —
#                     the kind is shared with decide()'s, the payload is not),
#                     new_predicate (a predicate spelling + fact_kind; no live filer since
#                     the two-tier cutover retired it), and shape_mismatch /
#                     split_proposal, which the CHECK admits but nothing in the backend
#                     files.
_FACT_PAYLOAD_KEYS = ("fact_id", "fact_a", "fact_b", "source_fact_id")

# The keys by which a settled review item's RECORDED EFFECTS name rows BY ID, in
# `resolution->'effects'` rather than in `payload` — a different shape, so a different
# arm rather than another entry above: these are id ARRAYS, and the rows they name are
# not facts the card points AT but rows its resolution MOVED. `merge_entity_pair`
# (analysis/entities.py) records exactly what the fold repointed and `_reverse_effects`
# (analysis/repo.py) moves those ids back one UPDATE at a time, so an un-merge is only
# as complete as the ids it can still find. Re-mint them and reopening a settled
# merge card restores the entity row (from `prior_status`/`prior_merged_into`, which
# are values, not ids) while moving ZERO mentions and ZERO facts — a half un-merge,
# silent, and worse than a clean no-op. `predicate_remapped` replays `fact_ids` the
# same way.
#
# Scalar `fact_id` effect keys (`retracted`, `pinned`, `domain_changed`) are NOT
# listed: every one of them names a row the card's own payload already names through
# `_FACT_PAYLOAD_KEYS`, or a derived shadow of one, which the spare set's
# `derived_from_fact_id` arm carries with its source.
_EFFECT_MENTION_KEYS = ("mention_ids",)
_EFFECT_FACT_KEYS = ("fact_ids", "object_fact_ids")


def _effect_named_ids_sql(keys: tuple[str, ...]) -> str:
    """A SELECT of every id `keys` name inside a SURVIVING review item's recorded
    effects. One arm per key, generated from the constants above so the spare set and
    the wipe can never read a different list. `jsonb_typeof` guards each hop: a NULL
    `resolution`, a non-array `effects`, or an effect missing the key yields no rows
    rather than erroring, so a card shape this function has never seen is inert."""
    return " UNION ".join(
        "SELECT arr.id AS id FROM app.review_items ri"
        " CROSS JOIN LATERAL jsonb_array_elements("
        "     CASE WHEN jsonb_typeof(ri.resolution->'effects') = 'array'"
        "          THEN ri.resolution->'effects' ELSE '[]'::jsonb END) AS eff(effect)"
        " CROSS JOIN LATERAL jsonb_array_elements_text("
        f"     CASE WHEN jsonb_typeof(eff.effect->'{key}') = 'array'"
        f"          THEN eff.effect->'{key}' ELSE '[]'::jsonb END) AS arr(id)"
        " WHERE ri.status NOT IN :purged"
        for key in keys
    )


async def rebuild_spare_mention_ids(session: AsyncSession, note_id: uuid.UUID) -> set[uuid.UUID]:
    """This note's entity mentions that a REBUILD must spare: the ones a surviving
    review item's effects will replay BY ID (`_EFFECT_MENTION_KEYS`).

    The fact spare set's twin, and for the identical reason — `_reverse_effects`
    repoints `app.entity_mentions` by id, so an id the purge re-mints is an un-merge
    that silently moves nothing. Empty for a privacy delete, which owes no replay: the
    note is gone and its card went with it.

    This is the *purge's* half of that promise. Whether a later re-analysis of the note
    preserves the ids it re-asserts is the mention writer's own contract; the purge must
    not be the thing that breaks it."""
    rows = await session.execute(
        text(
            "SELECT m.id FROM app.entity_mentions m WHERE m.note_id = :note"
            f" AND m.id::text IN (SELECT id FROM ({_effect_named_ids_sql(_EFFECT_MENTION_KEYS)}) e)"
        ).bindparams(bindparam("purged", expanding=True)),
        {"note": str(note_id), "purged": list(_REBUILD_PURGED_STATUSES)},
    )
    return {uuid.UUID(str(row[0])) for row in rows}


async def rebuild_spare_fact_ids(session: AsyncSession, note_id: uuid.UUID) -> set[uuid.UUID]:
    """This note's facts that a REBUILD must spare, gathered from three independent roots.

    A pinned fact is a human decision (an owner correction's force-supersede, or the
    side the owner picked resolving a review card), so it survives re-derivation. But
    sparing only the pinned row is not enough, for two unrelated reasons:

    1. **The chain below the pin.** The rows the pin superseded carry the verdict's
       shape. Purge one, re-derive it, and the fresh row lands beside the pin with no
       chain link, where `decide()` re-flags a pinned head it disagrees with
       ("Re-flag, never flip", analysis/supersession.py). Sparing the chain lets the
       re-derived twin match a surviving row by VALUE, so `decide()` takes its
       idempotent refresh branch instead.

    2. **Every fact a SURVIVING review item names, which no chain walk can reach.**
       Resolving a card writes no supersession edge at all: `repo.py` pins the winner
       (`superseded_by = NULL`) and marks the loser `status='retracted'`, leaving its
       `superseded_by` untouched — and the `low_confidence_inference` reject path does
       not even pin, it only retracts. Such a row is on no chain and carries no pin, so a
       `superseded_by`-only walk misses it. Deleting it strands the card on a dangling
       `payload` pointer (jsonb, no FK, so nothing errors) and makes reopen/undo a
       permanent silent no-op — `_reverse_effects` replays
       `UPDATE app.facts ... WHERE id = :id` against a row that is gone. So the facts a
       surviving item NAMES are spared directly, by id.

       Both halves of "surviving item" are derived, never enumerated by hand:
       `_REBUILD_PURGED_STATUSES` is the single list of what the purge deletes (the spare
       set is its complement, so `deferred` cannot be forgotten), and
       `_FACT_PAYLOAD_KEYS` is the single list of the keys a card names a fact by, read
       by `delete_review_items` too.

    3. **Every fact a surviving item's RECORDED EFFECTS will replay by id**
       (`_EFFECT_FACT_KEYS`), which the payload never names. A merge resolution's
       payload holds two ENTITY ids; the row ids the fold repointed live in
       `resolution->'effects'`, and that is what a reopen moves back. Purge them and the
       un-merge restores the entity row while moving zero facts — the same harm as (2),
       one level further out, and reached by a different shape (id arrays under
       `resolution`, not scalar keys under `payload`).

    Derived shadows of a spared fact are spared with it: a resolution cascades onto them
    and records their prior status in its effects (repo.py), so a shadow outliving its
    source's verdict is another dangling replay target.

    Sparing the loser is only half of the flood fix. `decide()` filters retracted rows
    out of `live` before matching, so a spared loser is invisible to it; the retracted-twin
    branch (analysis/supersession.py) is the half that consults this history. This
    function is the half that keeps it.

    **Honest limit.** That twin branch matches on EXACT `same_validity`, so a spared
    superseded/retracted row only absorbs the re-derived twin when re-extraction lands
    the same `valid_from`. Validity drift on re-extraction still drops through to the
    pinned re-flag and files a card. That is inherent to matching on value plus instant,
    not something this spare set can close.
    """
    # One `f.id::text IN (...)` arm per fact-naming payload key, generated from the
    # constant so the mapping above is the only place the key list lives.
    named_facts = ", ".join(f"ri.payload->>'{key}'" for key in _FACT_PAYLOAD_KEYS)
    effect_facts = _effect_named_ids_sql(_EFFECT_FACT_KEYS)
    rows = await session.execute(
        text(
            f"""
            WITH RECURSIVE walk(root, cur, depth) AS (
                SELECT f.id, f.id, 0 FROM app.facts f WHERE f.note_id = :note
                UNION ALL
                SELECT w.root, f.superseded_by, w.depth + 1
                FROM walk w JOIN app.facts f ON f.id = w.cur
                WHERE f.superseded_by IS NOT NULL AND w.depth < :cap
            ),
            pinned_roots AS (
                SELECT DISTINCT w.root AS id
                FROM walk w JOIN app.facts f ON f.id = w.cur
                WHERE f.pinned
            ),
            settled AS (
                SELECT f.id
                FROM app.review_items ri
                JOIN app.facts f ON f.id::text IN ({named_facts})
                WHERE ri.status NOT IN :purged AND f.note_id = :note
            ),
            unwound AS (
                SELECT f.id
                FROM app.facts f
                WHERE f.note_id = :note
                  AND f.id::text IN (SELECT id FROM ({effect_facts}) e)
            ),
            roots AS (
                SELECT id FROM pinned_roots
                UNION SELECT id FROM settled
                UNION SELECT id FROM unwound
            )
            SELECT id FROM roots
            UNION
            SELECT f.id FROM app.facts f JOIN roots r ON f.derived_from_fact_id = r.id
            WHERE f.note_id = :note
            """
        ).bindparams(bindparam("purged", expanding=True)),
        {
            "note": str(note_id),
            "cap": _CHAIN_DEPTH_CAP,
            "purged": list(_REBUILD_PURGED_STATUSES),
        },
    )
    keep = {uuid.UUID(str(row[0])) for row in rows}
    truncated = (
        await session.execute(
            text(
                """
                WITH RECURSIVE walk(cur, depth) AS (
                    SELECT f.id, 0 FROM app.facts f WHERE f.note_id = :note
                    UNION ALL
                    SELECT f.superseded_by, w.depth + 1
                    FROM walk w JOIN app.facts f ON f.id = w.cur
                    WHERE f.superseded_by IS NOT NULL AND w.depth < :cap
                )
                SELECT count(*) FROM walk WHERE depth >= :cap
                """
            ),
            {"note": str(note_id), "cap": _CHAIN_DEPTH_CAP},
        )
    ).scalar_one()
    if truncated:
        # Under-sparing is data loss; it must never be silent.
        log.warning("rebuild_spare_chain_capped", note_id=str(note_id), cap=_CHAIN_DEPTH_CAP)
    return keep


async def decision_retracted_fact_ids(session: AsyncSession, fact_ids: list[str]) -> set[str]:
    """Which of `fact_ids` a SETTLED review decision retracted — the evidence that a
    retracted row must never resurrect.

    `decide()` (analysis/supersession.py) has to tell two retractions apart. A row the
    machine retracted because re-extraction dropped its key MUST come back live when the
    key comes back; a row a human rejected must NOT. The retracted-twin branch used to
    discriminate on a PINNED head beside the row, because "no row records WHY a fact was
    retracted" — but a resolution does. `resolve_review` records
    `{"action": "retracted", "fact_id": ...}` on every path that retracts by decision
    (the losing side of a fact_conflict/attribute_collision, and the
    `low_confidence_inference` reject that pins nothing and so has no pinned head at
    all), and `_reverse_effects` reads those same effects to undo it. Reading the
    recorded effect is therefore reading the decision itself, not inferring it from a
    neighbouring row.

    Settled, not merely present: reopening a card leaves its effects on the row and
    flips the status back to `open` (repo.py), and the same
    `_REBUILD_PURGED_STATUSES` complement that decides which cards outlive a rebuild
    decides which decisions still hold — so a reopened decision stops holding on the
    same instant its card returns to the queue.

    Ids come back as text, matched against `FactView.id`; the caller passes only the
    retracted rows on one identity key, so the common case queries nothing at all.
    """
    if not fact_ids:
        return set()
    rows = await session.execute(
        text(
            "SELECT DISTINCT eff.effect->>'fact_id' AS id FROM app.review_items ri"
            " CROSS JOIN LATERAL jsonb_array_elements("
            "     CASE WHEN jsonb_typeof(ri.resolution->'effects') = 'array'"
            "          THEN ri.resolution->'effects' ELSE '[]'::jsonb END) AS eff(effect)"
            " WHERE ri.status NOT IN :purged AND eff.effect->>'action' = 'retracted'"
            " AND eff.effect->>'fact_id' IN :ids"
        ).bindparams(bindparam("purged", expanding=True), bindparam("ids", expanding=True)),
        {"purged": list(_REBUILD_PURGED_STATUSES), "ids": fact_ids},
    )
    return {str(row[0]) for row in rows}


async def purge_note_artifacts(
    session: AsyncSession, note_id: uuid.UUID, *, keep_pinned: bool = False
) -> PurgeCounts:
    """Purge every artifact derived from `note_id`, repairing supersession
    chains first. A never-analyzed note has nothing here and is a no-op.

    `keep_pinned=False` is the privacy delete this module exists for: total, down to
    resolved review history, agent episodes and ingest conversations.

    `keep_pinned=True` selects the REBUILD posture (analysis/rebuild.py) — the same
    destructive half with five deliberate exemptions, because a rebuild re-derives
    from notes that still exist rather than honoring a deletion promise:

    1. Facts a human verdict rests on survive (`rebuild_spare_fact_ids`): the pinned
       row, the chain it superseded, and — reachable by no chain walk — every fact named
       by a review item that outlives the purge, or named by ITS recorded effects.
       The entity MENTIONS those effects will replay survive with them
       (`rebuild_spare_mention_ids`): a reopen moves rows by id, so the wipe and the
       spare set must read the same source or an un-merge silently moves nothing.
    2. Only `_REBUILD_PURGED_STATUSES` review items go, the discipline the re-extraction
       sweep already uses (analysis/pipeline.py): resolved, dismissed and deferred items
       are HUMAN history or a parked decision. The `note_id` sweep is skipped for the
       same reason — it is status-blind by construction and exists to erase a deleted
       note's frozen snippets.
    3. Agent episodes stay. Nothing re-derives them, so purging them here would be
       silent data loss, not a rebuild.
    4. Note conversations stay, for the same reason and one stronger: a thread holds
       the OWNER'S answers to the agent's clarification questions, which no re-derive
       from the notes can reconstruct. Destroying a thread because the graph is being
       re-derived would throw away the human half of the ingest and orphan any question
       still waiting in the notes inbox.
    5. Clarification blocks stay. They are SOURCE — the owner typed them, and D7 makes
       them chunks of the note — so a rebuild that deleted them would leave the graph
       unable to re-derive from the notes, corpus-wide and silently. The whole premise
       of `keep_pinned=True` is re-deriving FROM the notes; a clarification is part of
       the note.
    """
    keep_ids = await rebuild_spare_fact_ids(session, note_id) if keep_pinned else set()
    keep_mentions = await rebuild_spare_mention_ids(session, note_id) if keep_pinned else set()
    all_facts = (
        await session.execute(
            select(
                Fact.id, Fact.superseded_by, Fact.valid_from, Fact.entity_id, Fact.object_entity_id
            ).where(Fact.note_id == note_id)
        )
    ).all()
    doomed = [f for f in all_facts if f.id not in keep_ids]
    doomed_links: dict[uuid.UUID, uuid.UUID | None] = {f.id: f.superseded_by for f in doomed}
    doomed_close: dict[uuid.UUID, datetime | None] = {f.id: f.valid_from for f in doomed}

    # Entity ids must be collected BEFORE their referencing rows go: after
    # the deletes there is no way to know which entities this note touched.
    candidates: set[uuid.UUID] = set()
    for f in doomed:
        candidates.add(f.entity_id)
        if f.object_entity_id is not None:
            candidates.add(f.object_entity_id)
    candidates.update(
        (
            await session.execute(
                select(EntityMention.entity_id).where(EntityMention.note_id == note_id).distinct()
            )
        ).scalars()
    )

    await repair_chains(session, doomed_links, doomed_close)
    await delete_review_items(
        session,
        set(doomed_links),
        note_id=None if keep_pinned else note_id,
        statuses=_REBUILD_PURGED_STATUSES if keep_pinned else None,
    )

    fact_delete = delete(Fact).where(Fact.note_id == note_id)
    if keep_ids:
        fact_delete = fact_delete.where(Fact.id.not_in(keep_ids))
    await session.execute(fact_delete)
    # A token a SPARED fact still cites outlives the purge — the FK would abort the
    # delete otherwise, and `_upsert_tokens` is get-or-create keyed on (phrase,
    # resolved start), so re-extraction reuses the surviving row instead of
    # duplicating it. Empty for a privacy delete: every one of the note's tokens goes.
    doomed_tokens = select(TemporalToken.id).where(TemporalToken.note_id == note_id)
    if keep_ids:
        doomed_tokens = doomed_tokens.where(
            TemporalToken.id.not_in(
                select(Fact.temporal_token_id).where(
                    Fact.id.in_(keep_ids), Fact.temporal_token_id.is_not(None)
                )
            )
        )
    # Tokens are per-note by construction (the pipeline only mints them for
    # the note being analyzed), so no other note's fact should cite one — but
    # the FK would abort the whole delete if a stray citation ever appeared,
    # so unhook defensively rather than trust the invariant. Runs after the
    # fact delete: only surviving facts can still match.
    await session.execute(
        update(Fact).where(Fact.temporal_token_id.in_(doomed_tokens)).values(temporal_token_id=None)
    )
    await session.execute(delete(TemporalToken).where(TemporalToken.id.in_(doomed_tokens)))
    mention_delete = delete(EntityMention).where(EntityMention.note_id == note_id)
    if keep_mentions:
        mention_delete = mention_delete.where(EntityMention.id.not_in(keep_mentions))
    await session.execute(mention_delete)
    await session.execute(delete(NoteAnalysis).where(NoteAnalysis.note_id == note_id))
    await _delete_orphaned_entities(session, candidates)
    # An orphaned appointment entity cascaded out with its row; a SURVIVING one
    # (still mentioned elsewhere) may have lost this note's scheduledTime, so
    # re-derive its projection — the row is removed when no live time remains.
    await project_appointments(session, candidates)
    await project_emr(session, candidates)
    await project_place_geofences(session, candidates)
    if not keep_pinned:
        await _purge_clarifications(session, note_id)
        await _purge_episodes(session, note_id)
        await _purge_conversations(session, note_id)
    return PurgeCounts(purged=len(doomed), kept=len(keep_ids))


async def _purge_clarifications(session: AsyncSession, note_id: uuid.UUID) -> None:
    """Delete the note's clarification blocks (D6, migration 0193).

    This runs on the PRIVACY delete only — never under `keep_pinned=True`, see
    exemption 5 above. It has to be an explicit statement rather than the table's
    ON DELETE CASCADE for the same reason `_purge_episodes` does: deleting a note is a
    SOFT delete that keeps the `app.notes` row (invariant #11), so the cascade never
    fires on the one path that needs it.

    Placed here rather than in `SqlNotesRepo.delete_note` because this is the second
    reaper too: `backfill_deleted_note_artifacts` sweeps notes soft-deleted by paths
    that never touch the repo (an intake link's teardown, intake/repo.py), and a
    clarification left behind by one of those is owner-typed text surviving a deletion
    promise.
    """
    await session.execute(delete(NoteClarification).where(NoteClarification.note_id == note_id))


async def _purge_episodes(session: AsyncSession, note_id: uuid.UUID) -> None:
    """Purge is total (invariant #11): an episodic trace derived from this note is
    deleted WHOLE — the episode row, not merely its pointer — so no agent-memory
    row retains content derived from a deleted note. Refs cascade with the episode
    (the note FK can't: notes soft-delete, so its ON DELETE CASCADE never fires)."""
    await session.execute(
        delete(AgentEpisode).where(
            AgentEpisode.id.in_(
                select(AgentEpisodeRef.episode_id).where(AgentEpisodeRef.note_id == note_id)
            )
        )
    )


async def _purge_conversations(session: AsyncSession, note_id: uuid.UUID) -> None:
    """Delete the note's ingest conversations WHOLE — the `agent_sessions` row, not just
    the `note_conversations` side row — so no transcript keeps the deleted note's body
    or the owner's answers about it. The same reasoning as `_purge_episodes`, and the
    reason `note_conversations` carries no DELETE grant: erasing the side row alone
    would strand exactly the content the promise is about.

    Explicit rather than a cascade: `app.notes` soft-deletes (notes/repo.py), so the
    `note_conversations.note_id` FK's ON DELETE CASCADE never fires on a note delete
    (constraint 11 of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md). Deleting the
    session cascades the side row, the tool-call ledger, the turns, and the runs (0021).

    The blast radius is therefore a WHOLE session, and the invariant that keeps it
    correct lives at the other end: `NoteConversationRepo.start` is opened on a session
    created FOR the note. D1's "same agent, same loop, same memory" is about the agent,
    not about reusing an `agent_sessions` row — attaching a conversation to a running
    Full Brain chat would put that chat's entire history inside this DELETE. Stated
    rather than guarded because the only structural guard available in W2 (refuse a
    session that already has turns) would encode an ordering W3's loop has not chosen
    yet, and a guard that fails closed on the legitimate path is unfixable on a box
    with no terminal (CLAUDE.md #10).
    """
    await session.execute(
        delete(AgentSession).where(
            AgentSession.id.in_(
                select(NoteConversation.session_id).where(NoteConversation.note_id == note_id)
            )
        )
    )


async def repair_chains(
    session: AsyncSession,
    doomed_links: dict[uuid.UUID, uuid.UUID | None],
    doomed_close: dict[uuid.UUID, datetime | None],
) -> None:
    """Fix surviving facts whose supersessor is doomed — purged by note
    deletion, or retracted by a re-extraction sweep (analysis/pipeline.py),
    which is why the survivor filter is "not itself doomed" rather than
    "other note": intra-note chains exist, and a same-note fact the sweep
    left alone still deserves repair.

    A survivor whose chain re-attaches to a NON-doomed fact deeper down stays
    superseded — the world still moved past it, just with one less link of
    evidence — so only the dangling pointer is repaired. A survivor whose
    chain dies inside the doomed set is restored: the only evidence it was
    ever superseded is being deleted (or retracted).
    """
    if not doomed_links:
        return
    survivors = (
        await session.execute(
            select(Fact.id, Fact.superseded_by, Fact.status, Fact.valid_to).where(
                Fact.superseded_by.in_(doomed_links), Fact.id.not_in(doomed_links)
            )
        )
    ).all()
    for s in survivors:
        target = chain_repair_target(s.superseded_by, doomed_links)
        if target is not None:
            # valid_to is left alone: the close may have copied the doomed
            # middle link's valid_from, but the surviving supersessor still
            # bounds the interval and inventing a new close here would be
            # guessing.
            await session.execute(update(Fact).where(Fact.id == s.id).values(superseded_by=target))
            continue
        values: dict[str, Any] = {"superseded_by": None}
        # Only a supersession is undone. retracted/pending_review are
        # verdicts about THIS fact (a human's, or re-extraction's), not
        # consequences of the doomed fact, so they survive with the dangling
        # link cleared.
        if s.status == "superseded":
            values["status"] = "active"
        # Honest approximation: the SCD-2 close copies the dooming fact's
        # valid_from, so equality is our only evidence the close came FROM
        # that fact rather than from the survivor's own note. On a match the
        # interval reopens; a coincidentally equal independent close would
        # reopen too, and we accept that over keeping an unevidenced close.
        if s.valid_to is not None and s.valid_to == doomed_close.get(s.superseded_by):
            values["valid_to"] = None
        await session.execute(update(Fact).where(Fact.id == s.id).values(**values))


async def delete_review_items(
    session: AsyncSession,
    doomed_ids: set[uuid.UUID],
    *,
    note_id: uuid.UUID | None = None,
    statuses: tuple[str, ...] | None = None,
) -> None:
    """Delete review items referencing doomed facts (every key in
    `_FACT_PAYLOAD_KEYS`), plus — when `note_id` is given — everything filed for
    the note itself.

    The privacy purge passes note_id and no status filter: resolved history is
    derived data too, and its frozen display snippets quote the note's text.
    Items created by another note but referencing a doomed fact go as well,
    including the one-doomed-one-surviving case: such a card is unservable
    (one side's evidence is gone) and its choice labels quote the doomed
    fact's statement. The re-extraction sweep and the rebuild sweep instead pass
    statuses=('open',) and no note_id: resolved/dismissed items are HUMAN
    history and survive a re-run.
    """
    status_clause = " AND status IN :statuses" if statuses is not None else ""
    if note_id is not None:
        stmt = text(
            f"DELETE FROM app.review_items WHERE payload->>'note_id' = :note{status_clause}"
        )
        params: dict[str, Any] = {"note": str(note_id)}
        if statuses is not None:
            stmt = stmt.bindparams(bindparam("statuses", expanding=True))
            params["statuses"] = list(statuses)
        await session.execute(stmt, params)
    if not doomed_ids:
        return
    ids = [str(d) for d in doomed_ids]
    # One arm per fact-naming key, generated from the same constant the rebuild spare set
    # reads — so a card can never be deleted by a key the spare set does not consult.
    keys = " OR ".join(
        f"payload->>'{key}' IN :doomed_{i}" for i, key in enumerate(_FACT_PAYLOAD_KEYS)
    )
    stmt = text(f"DELETE FROM app.review_items WHERE ({keys}){status_clause}").bindparams(
        *(bindparam(f"doomed_{i}", expanding=True) for i in range(len(_FACT_PAYLOAD_KEYS)))
    )
    params = {f"doomed_{i}": ids for i in range(len(_FACT_PAYLOAD_KEYS))}
    if statuses is not None:
        stmt = stmt.bindparams(bindparam("statuses", expanding=True))
        params["statuses"] = list(statuses)
    await session.execute(stmt, params)


def _orphan_conditions() -> list[Any]:
    """The criteria for a provisional entity that no surviving knowledge references —
    safe to hard-delete. Shared by the per-note purge (`_delete_orphaned_entities`)
    and the periodic global sweep (`sweep_orphaned_entities`), so they can never
    diverge: never an entity that is confirmed/subject-linked (the "Me" entity is
    sacrosanct), still mentioned or cited by a surviving fact, held by a distinct_from
    edge, or pointed at by a merge tombstone — that knowledge outlives any one note."""
    tombstone = aliased(Entity)
    return [
        Entity.status == "provisional",
        Entity.subject_id.is_(None),
        ~select(EntityMention.id).where(EntityMention.entity_id == Entity.id).exists(),
        ~select(Fact.id).where(Fact.entity_id == Entity.id).exists(),
        ~select(Fact.id).where(Fact.object_entity_id == Entity.id).exists(),
        ~select(EntityDistinction.id)
        .where(
            (EntityDistinction.entity_a == Entity.id) | (EntityDistinction.entity_b == Entity.id)
        )
        .exists(),
        ~select(tombstone.id).where(tombstone.merged_into_id == Entity.id).exists(),
    ]


async def _delete_orphaned_entities(session: AsyncSession, candidates: set[uuid.UUID]) -> None:
    """Provisional entities that existed only because of this note vanish;
    their aliases cascade. Restricts the shared orphan criteria to this note's
    candidate entities (`_orphan_conditions`)."""
    if not candidates:
        return
    await session.execute(delete(Entity).where(Entity.id.in_(candidates), *_orphan_conditions()))


async def sweep_orphaned_entities(
    maker: async_sessionmaker[AsyncSession], *, min_age_hours: int = 1, ctx: Any = None
) -> int:
    """The periodic global orphan sweep (the `entity_hygiene` action): delete EVERY
    provisional entity matching the shared orphan criteria, not only those tied to a
    just-deleted note. Closes the gap where a fact retraction or supersession strands a
    provisional entity (zero mentions/facts/edges) that the per-note purge never visits.

    Unlike the per-note purge (which acts on a just-deleted note's candidates, never racing
    live extraction), this runs corpus-wide and can fire — manually from Ops — *during* an
    extraction. So it adds an **age guard**: only entities older than `min_age_hours` are
    eligible, so a provisional entity an in-flight extraction just inserted but has not yet
    linked to its mention/fact is never deleted out from under it. Runs under SYSTEM_CTX;
    returns the count deleted. Idempotent — a second run finds nothing once the backlog
    clears."""
    from datetime import UTC, timedelta
    from typing import cast

    from sqlalchemy.engine import CursorResult

    from jbrain.db.session import scoped_session
    from jbrain.queue import SYSTEM_CTX

    cutoff = datetime.now(UTC) - timedelta(hours=min_age_hours)
    # Default SYSTEM_CTX (all domains); a narrowed ctx is firewalled by RLS to its scope,
    # so a domain-scoped sweep can only ever delete in-scope orphans (the firewall test).
    async with scoped_session(maker, ctx or SYSTEM_CTX) as session:
        result = await session.execute(
            delete(Entity).where(*_orphan_conditions(), Entity.created_at < cutoff)
        )
    return cast("CursorResult[Any]", result).rowcount or 0


async def backfill_deleted_note_artifacts(
    maker: async_sessionmaker[AsyncSession],
) -> int:
    """One-shot startup sweep: purge artifacts of notes deleted BEFORE the
    cascade existed. Idempotent — a fully purged note matches no candidate
    predicate — so running it every worker boot costs one cheap query once
    the backlog is clear."""
    from jbrain.db.session import scoped_session
    from jbrain.queue import SYSTEM_CTX

    candidate_sql = text(
        """
        SELECT n.id FROM app.notes n
        WHERE n.deleted_at IS NOT NULL AND (
            EXISTS (SELECT 1 FROM app.facts f WHERE f.note_id = n.id)
            OR EXISTS (SELECT 1 FROM app.entity_mentions m WHERE m.note_id = n.id)
            OR EXISTS (SELECT 1 FROM app.temporal_tokens t WHERE t.note_id = n.id)
            OR EXISTS (SELECT 1 FROM app.note_analysis a WHERE a.note_id = n.id)
            OR EXISTS (
                SELECT 1 FROM app.review_items r
                WHERE r.payload->>'note_id' = n.id::text
            )
            -- The artifacts `purge_note_artifacts` deletes WHOLE. Without them a note
            -- whose only surviving artifact is an episode, an ingest conversation or a
            -- clarification block matches nothing and is never swept, so the idempotence
            -- claim above ("a fully purged note matches no candidate predicate") would
            -- be true only because the sweep cannot see what it left behind.
            OR EXISTS (SELECT 1 FROM app.agent_episode_refs er WHERE er.note_id = n.id)
            OR EXISTS (SELECT 1 FROM app.note_conversations c WHERE c.note_id = n.id)
            OR EXISTS (SELECT 1 FROM app.note_clarifications c WHERE c.note_id = n.id)
        )
        """
    )
    async with scoped_session(maker, SYSTEM_CTX) as session:
        candidates = [row[0] for row in (await session.execute(candidate_sql)).all()]
    for note_id in candidates:
        async with scoped_session(maker, SYSTEM_CTX) as session:
            await purge_note_artifacts(session, uuid.UUID(str(note_id)))
            await session.commit()
    return len(candidates)
